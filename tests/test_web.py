"""The web interface: events stream to the page, instructions post back, the session runs them in order."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from rich.console import Console

from prax_bot.runs import load_run, reopen_workspace
from prax_bot.session import Session, new_run
from prax_bot.web import PAGE, WebSession, make_handler
from tests.test_agent import FakeLLM, call, reply
from tests.test_resume import make_snapshot
from tests.test_runs import make_run, settings


def test_web_session_runs_instructions_and_streams_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.llm

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    llm = FakeLLM(
        [reply(None, call("write_file", path="b.py", content="1\n")), reply(None, call("finish", summary="wrote b"))]
    )
    monkeypatch.setattr(prax_bot.llm, "LLM", lambda settings: llm)

    def make_session(out: Console, hook):
        ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, hook, "20260902T000000Z-web")
        run_id, run_dir, events_file, on_event = new_run(s, verbose=False, out=out)
        return Session(
            s,
            ws,
            tmp_path,
            backend="local",
            run_id=run_id,
            run_dir=run_dir,
            events_file=events_file,
            on_event=lambda kind, **p: (on_event(kind, **p), hook(kind, **p)),
            protect=[],
            read_line=lambda: None,
            out=out,
        )

    web = WebSession(s, tmp_path, "local", make_session)
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
            and state["files"] == ["b.py"]
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

        req = urllib.request.Request(
            f"{base}/quit?token=tok", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        assert web.closed
    finally:
        server.shutdown()
