"""The web interface: events stream to the page, instructions post back, the session runs them in order."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from rich.console import Console

from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.ui.common import create_session
from prax_bot.ui.web import PAGE, WebSession, make_handler
from tests.fakes import FakeLLM, call, reply
from tests.helpers import git_repo, make_run, make_snapshot, settings


def test_web_session_runs_instructions_and_streams_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm

    s = dataclasses.replace(settings(tmp_path), backend="local")
    root = git_repo(tmp_path / "repo")
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    llm = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="1\n")),
            reply(None, call("write_file", path="test_new.py", content="new test\n")),
            reply(None, call("finish", summary="wrote b")),
        ]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)

    ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, None, "prepared-ui")
    monkeypatch.setattr("prax_bot.runtime.factory.open_workspace", lambda *args: ws)

    def make_session(out: Console, hook):
        session = create_session(s, root, out=out, hook=hook)
        return session

    web = WebSession(s, root, "local", make_session)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(web, "tok", PAGE))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for _ in range(100):
            if web.session is not None:
                break
            time.sleep(0.05)
        assert web.session is not None
        page = urllib.request.urlopen(f"{base}/").read().decode()
        assert "Prax Bot" in page and "tok" in page and "/goal" in page
        script = urllib.request.urlopen(f"{base}/assets/app.js")
        assert script.headers["Content-Type"] == "text/javascript" and b"EventSource" in script.read()
        css = urllib.request.urlopen(f"{base}/assets/app.css")
        assert css.headers["Content-Type"] == "text/css" and css.read()
        state = json.loads(urllib.request.urlopen(f"{base}/state").read())
        assert state["status"] == "idle" and state["rounds"] == 0

        forbidden = urllib.request.Request(
            f"{base}/send", data=b'{"text": "x"}', headers={"Content-Type": "application/json"}, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(forbidden)
        assert err.value.code == 403, "posting needs the token"

        req = urllib.request.Request(
            f"{base}/send?token=tok",
            data=json.dumps({"text": "add b"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        for _ in range(200):
            state = json.loads(urllib.request.urlopen(f"{base}/state").read())
            if state["rounds"] == 1 and state["status"] == "idle":
                break
            time.sleep(0.05)
        assert (
            state["rounds"] == 1
            and state["files"] == ["b.py", "test_new.py"]
            and state["summary"] == "wrote b"
            and "b/b.py" in state["diff"]
        )

        stream = urllib.request.urlopen(f"{base}/events?token=tok", timeout=5)
        kinds: list[str] = []
        deadline = time.time() + 5
        while time.time() < deadline:
            line = stream.readline().decode()
            if line.startswith("data: "):
                event = json.loads(line[6:])
                kinds.append(event["kind"])
                if event["kind"] == "state" and event.get("rounds") == 1 and kinds.count("state") > 1:
                    break
        assert (
            "instruction.start" in kinds
            and "tool.call" in kinds
            and "tool.result" in kinds
            and "finish" in kinds
            and "instruction.done" in kinds
        )
        assert (
            kinds.index("instruction.start")
            < kinds.index("tool.call")
            < kinds.index("finish")
            < kinds.index("instruction.done")
        )
        stream.close()

        req = urllib.request.Request(
            f"{base}/apply?token=tok",
            data=b'{"files": ["b.py"]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        for _ in range(200):
            state = json.loads(urllib.request.urlopen(f"{base}/state").read())
            if state["files"] == ["test_new.py"] and state["status"] == "idle":
                break
            time.sleep(0.02)
        assert state["files"] == ["test_new.py"] and not state["can_undo"]
        assert (root / "b.py").read_text() == "1\n" and not (root / "test_new.py").exists()

        cursor = web.seq
        web.emit({"kind": "replay-marker"})
        replay = urllib.request.urlopen(
            urllib.request.Request(f"{base}/events?token=tok", headers={"Last-Event-ID": str(cursor)}), timeout=5
        )
        assert replay.readline().decode().strip() == f"id: {cursor + 1}"
        event = json.loads(replay.readline().decode().removeprefix("data: "))
        assert event["kind"] == "replay-marker", "reconnect must not replay already received events"
        replay.close()

        req = urllib.request.Request(
            f"{base}/quit?token=tok", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        web.worker.join(timeout=5)
        assert web.closed
    finally:
        server.shutdown()
