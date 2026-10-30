"""Named web sessions keep independent workers and continue their saved work."""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from prax_bot.runtime.factory import create_session
from prax_bot.runtime.runs import load_run
from prax_bot.ui.web import PAGE, WebSession, make_handler
from prax_bot.ui.web_sessions import WebSessions
from prax_bot.workspace.local import LocalWorkspace, clone_dir
from tests.fakes import FakeLLM, call, reply
from tests.helpers import git_repo, settings


@pytest.fixture
def session_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = dataclasses.replace(settings(tmp_path), backend="local")
    root = git_repo(tmp_path / "repo")

    def workspace(config, root, run_id, on_event):
        directory = config.home / "local" / run_id
        directory.mkdir(parents=True)
        clone_dir(root, directory / "current")
        return LocalWorkspace(directory, command_timeout=config.command_timeout, on_event=on_event)

    monkeypatch.setattr("prax_bot.runtime.factory.open_workspace", workspace)

    def factory(out, hook, past, extra_tools):
        return create_session(
            config,
            root,
            on_event=hook,
            plugins=extra_tools,
            resume=(past, len(past.checkpoints) - 1) if past is not None else None,
            continue_session=past is not None,
            llm=FakeLLM([]),
        )

    return config, root, factory


@pytest.fixture
def manager(session_factory):
    config, root, factory = session_factory
    manager = WebSessions(config, root, "local", factory)
    try:
        yield manager
    finally:
        manager.close()
        manager.join()


@pytest.fixture
def server(manager: WebSessions):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, "tok", PAGE))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def ready(web: WebSession) -> WebSession:
    assert web.idle.wait(5), "session worker did not become idle"
    assert web.state()["status"] == "idle", web.state()
    assert web.session is not None
    return web


def request(base: str, path: str, body: dict[str, Any] | None = None, *, session: str | None = None):
    query = {"token": "tok"}
    if session is not None:
        query["session"] = session
    req = urllib.request.Request(
        f"{base}{path}?{urllib.parse.urlencode(query)}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def tool(main: WebSession, tool_name: str, **arguments: Any) -> dict[str, Any]:
    selected = next(tool for tool in main.session.plugins if tool.name == tool_name)
    return json.loads(selected.handler(main.session.ws, selected.model.model_validate(arguments)))


class PausedModel(FakeLLM):
    def __init__(self, filename: str):
        super().__init__(
            [
                reply(None, call("write_file", path=filename, content=f"{filename}\n")),
                reply(None, call("finish", summary=f"added {filename}")),
            ]
        )
        self.entered, self.release = threading.Event(), threading.Event()

    def complete(self, *args, **kwargs):
        if len(self.requests) == 1:
            self.entered.set()
            assert self.release.wait(10), "test did not release the model call"
        return super().complete(*args, **kwargs)


def test_main_agent_delegates_and_reads_work_without_applying_it(session_factory) -> None:
    config, root, factory = session_factory

    class Coordinator(FakeLLM):
        def complete(self, messages, tools, **kwargs):
            self.usage.calls += 1
            if self.usage.calls == 1:
                start = call("session_start")
                start.function.arguments = json.dumps({"name": "Delegated fix", "task": "Create fixed.py"})
                return reply(None, start)
            if self.usage.calls == 2:
                output = next(message["content"] for message in reversed(messages) if message["role"] == "tool")
                session_id = json.loads(output.splitlines()[0])["session_id"]
                return reply(None, call("session_read", session_id=session_id, wait_seconds=5))
            return reply(None, call("finish", summary="Delegated fix is ready for review. No host changes applied."))

    def scripted(out, hook, past, extra_tools):
        session = factory(out, hook, past, extra_tools)
        session.llm = (
            Coordinator([])
            if extra_tools
            else FakeLLM(
                [
                    reply(None, call("write_file", path="fixed.py", content="fixed\n")),
                    reply(None, call("finish", summary="Created fixed.py")),
                ]
            )
        )
        return session

    manager = WebSessions(config, root, "local", scripted)
    try:
        main = ready(manager.get())
        main.submit("Delegate a fix and inspect the result", source_run=main.session.run_id)
        assert main.idle.wait(10)
        assert main.session.last_result.finished
        entries = manager.listing()["sessions"]
        child = next(entry for entry in entries if not entry["is_main"])
        work = ready(manager.get(child["id"]))
        assert work.session.ws.read("fixed.py") == b"fixed\n"
        assert not (root / "fixed.py").exists()
        assert not main.state()["files"]
        assert "review" in main.state()["summary"]
        saved = load_run(main.session.run_id, config)
        outputs = [event for event in saved.events if event.get("kind") == "tool.result"]
        assert any('"files": ["fixed.py"]' in event["output"] for event in outputs)
    finally:
        manager.close()
        manager.join()


def test_reopening_replays_web_commands_and_agent_turns_once(manager: WebSessions) -> None:
    session_id = manager.create("Transcript")
    worker = ready(manager.get(session_id))
    worker.session.llm = FakeLLM(
        [
            reply(None, call("write_file", path="history.py", content="saved\n")),
            reply(None, call("finish", summary="Created history.py")),
        ]
    )
    worker.submit("Create history.py", source_run=worker.session.run_id)
    ready(worker)
    worker.submit("/diff", source_run=worker.session.run_id)
    ready(worker)
    manager.close_session(session_id, worker.session.run_id)
    worker.worker.join(5)
    manager.open(session_id)
    reopened = ready(manager.get(session_id))
    starts = [event["text"] for event in reopened.history if event["kind"] == "instruction.start"]
    assert starts == ["Create history.py", "/diff"]
    assert any(event["kind"] == "tool.result" for event in reopened.history)
    assert reopened.state()["files"] == ["history.py"]


def test_workers_and_workspaces_are_independent_and_switching_does_not_stop(manager: WebSessions) -> None:
    first = ready(manager.get(manager.create("First")))
    second = ready(manager.get(manager.create("Second")))
    first_model, second_model = PausedModel("first.py"), PausedModel("second.py")
    first.session.llm, second.session.llm = first_model, second_model
    try:
        first.submit("Add first file", source_run=first.session.run_id)
        second.submit("Add second file", source_run=second.session.run_id)
        assert first_model.entered.wait(5) and second_model.entered.wait(5)
        assert first.busy and second.busy
        assert ready(manager.get()).state()["is_main"] is True
        assert manager.get(first.state()["session_id"]) is first
        assert manager.get(second.state()["session_id"]) is second
        assert not first.session.cancel.is_set() and not second.session.cancel.is_set()
        assert first.stop()
        first_model.release.set()
        ready(first)
        assert first.session.last_result.reason == "cancelled"
        assert second.busy and not second.session.cancel.is_set()
        second_model.release.set()
        ready(second)
        assert first.state()["files"] == ["first.py"]
        assert second.state()["files"] == ["second.py"]
        assert first.session.ws.root != second.session.ws.root
    finally:
        first_model.release.set()
        second_model.release.set()


def test_main_and_names_survive_restart_with_latest_checkpoint_and_history(
    manager: WebSessions, session_factory
) -> None:
    config, root, factory = session_factory
    main = ready(manager.get())
    main_id, main_run_id = manager.main_id, main.session.run_id
    work_id = manager.create("Original name")
    work = ready(manager.get(work_id))
    work.session.llm = FakeLLM(
        [
            reply(None, call("write_file", path="kept.py", content="saved\n")),
            reply(None, call("finish", summary="Saved file")),
        ]
    )
    work.submit("Keep this work", source_run=work.session.run_id)
    ready(work)
    old_run_id, old_rounds = work.session.run_id, work.session.rounds
    manager.rename(work_id, "Continue later")
    manager.close()
    manager.join()
    restarted = WebSessions(config, root, "local", factory)
    try:
        assert restarted.main_id == main_id
        restored_main = ready(restarted.get())
        assert restored_main.session.run_id != main_run_id
        listing = restarted.listing()
        assert next(entry for entry in listing["sessions"] if entry["id"] == work_id)["name"] == "Continue later"
        before_read = set((config.home / "runs").iterdir())
        saved_state = tool(restored_main, "session_read", session_id=work_id)
        assert saved_state["status"] == "saved" and "kept.py" in saved_state["diff"]
        assert set((config.home / "runs").iterdir()) == before_read, "reading saved work must not create a workspace"
        restored = ready(restarted.get(work_id))
        assert restored.session.run_id != old_run_id
        assert restored.session.resumed_from["run_id"] == old_run_id
        assert restored.session.ws.read("kept.py") == b"saved\n"
        assert restored.session.rounds == old_rounds
        assert restored.session.first_task == "Keep this work"
        assert restored.state()["summary"] == "Saved file"
        assert restored.state()["session_name"] == "Continue later"
        assert any(
            event.get("kind") == "instruction.start" and event.get("text") == "Keep this work"
            for event in restored.history
        )
        assert not restored.busy and restored.pending.empty(), "opening saved work must not resume its job"
        lineage = next(entry for entry in restarted.listing()["sessions"] if entry["id"] == work_id)["run_ids"]
        assert lineage == [old_run_id, restored.session.run_id]
    finally:
        restarted.close()
        restarted.join()


def test_http_selects_sessions_and_rejects_stale_run_after_reopen(manager: WebSessions, server: str) -> None:
    work_id = manager.create("Work")
    work = ready(manager.get(work_id))
    source_run = work.session.run_id
    status, state = request(server, "/state", session=work_id)
    assert status == 200 and state["session_id"] == work_id
    assert state["main_session_id"] == manager.main_id and state["managed"] and not state["is_main"]
    status, main_state = request(server, "/state")
    assert status == 200 and main_state["session_id"] == manager.main_id
    status, _ = request(server, "/sessions/close", {"session_id": work_id, "source_run": source_run})
    assert status == 200
    work.worker.join(5)
    assert work.closed
    assert manager.get(work_id) is work
    status, closed_state = request(server, "/state", session=work_id)
    assert status == 200 and closed_state["status"] == "closed" and closed_state["run_id"] == source_run
    status, _ = request(server, "/send", {"text": "closed instruction", "source_run": source_run}, session=work_id)
    assert status == 409 and manager.get(work_id) is work
    status, body = request(server, "/sessions/open", {"session_id": work_id})
    assert status == 200 and body["session_id"] == work_id
    reopened = ready(manager.get(work_id))
    assert reopened.session.run_id != source_run
    for path, mutation in (
        ("/send", {"text": "stale instruction"}),
        ("/apply", {"files": []}),
        ("/compare", {"objective": "stale comparison"}),
        ("/choose", {"candidate": "A", "comparison_id": "stale-comparison"}),
        ("/stop", {}),
        ("/quit", {}),
    ):
        status, _ = request(server, path, {**mutation, "source_run": source_run}, session=work_id)
        assert status == 409, path
    status, _ = request(server, "/send", {"text": "missing source"}, session=work_id)
    assert status == 409
    assert not reopened.closing and reopened.pending.empty() and reopened.session.rounds == 0
    reopened.submit("stale queued instruction", source_run=source_run)
    ready(reopened)
    assert reopened.session.rounds == 0 and reopened.session.llm.requests == []
    reopened.session.llm = FakeLLM([reply(None, call("finish", summary="fresh"))])
    status, _ = request(
        server, "/send", {"text": "fresh instruction", "source_run": reopened.session.run_id}, session=work_id
    )
    assert status == 200
    ready(reopened)
    assert reopened.session.rounds == 1


def test_events_streams_and_reconnect_cursors_are_per_session(manager: WebSessions, server: str) -> None:
    first = ready(manager.get(manager.create("First")))
    second = ready(manager.get(manager.create("Second")))
    for selected, other, marker in ((first, second, "first-only"), (second, first, "second-only")):
        cursor = selected.seq
        other.emit({"kind": "marker", "text": "other-session"})
        selected.emit({"kind": "marker", "text": marker})
        query = urllib.parse.urlencode({"token": "tok", "session": selected.state()["session_id"]})
        req = urllib.request.Request(f"{server}/events?{query}", headers={"Last-Event-ID": str(cursor)})
        with urllib.request.urlopen(req, timeout=5) as stream:
            assert stream.readline().decode().strip() == f"id: {cursor + 1}"
            event = json.loads(stream.readline().decode().removeprefix("data: "))
            assert event["kind"] == "marker" and event["text"] == marker


def test_main_cannot_be_closed(manager: WebSessions, server: str) -> None:
    main = ready(manager.get())
    with pytest.raises(RuntimeError, match=r"[Mm]ain"):
        manager.close_session(manager.main_id, main.session.run_id)
    status, _ = request(server, "/quit", {"source_run": main.session.run_id}, session=manager.main_id)
    assert status in (400, 409)
    main.submit("/quit", source_run=main.session.run_id)
    ready(main)
    assert not main.closed and not main.closing and main.worker.is_alive()


def test_import_validates_repository_backend_and_live_writer(
    manager: WebSessions, session_factory, server: str
) -> None:
    config, root, factory = session_factory
    standalone = factory(None, lambda *args, **kwargs: None, None, [])
    run_id = standalone.run_id
    try:
        with pytest.raises(RuntimeError, match=r"live|active|open|running"):
            manager.import_run(run_id)
    finally:
        standalone.close()
    path = standalone.run_dir / "run.json"
    saved = json.loads(path.read_text())
    assert any(run["run_id"] == run_id for run in manager.listing()["saved_runs"])
    path.write_text(json.dumps({**saved, "repo": str(root / "another")}))
    with pytest.raises(ValueError, match="repository"):
        manager.import_run(run_id)
    assert not any(run["run_id"] == run_id for run in manager.listing()["saved_runs"])
    path.write_text(json.dumps({**saved, "backend": "contree"}))
    with pytest.raises(ValueError, match="backend"):
        manager.import_run(run_id)
    path.write_text(json.dumps(saved))
    status, imported_body = request(server, "/sessions/import", {"run_id": run_id, "name": "Imported work"})
    assert status == 200
    imported_id = imported_body["session_id"]
    imported = ready(manager.get(imported_id))
    assert imported.state()["session_name"] == "Imported work"
    assert imported.session.resumed_from["run_id"] == run_id
    assert load_run(run_id, config).info["repo"] == str(root)
    assert manager.import_run(run_id) == imported_id


def test_existing_saved_run_is_listed_without_becoming_main(session_factory) -> None:
    config, root, factory = session_factory
    saved = factory(None, lambda *args, **kwargs: None, None, [])
    saved.close()
    manager = WebSessions(config, root, "local", factory)
    try:
        main = ready(manager.get())
        assert main.session.resumed_from is None and main.session.run_id != saved.run_id
        listing = manager.listing()
        assert len(listing["sessions"]) == 1
        assert listing["sessions"][0]["id"] == manager.main_id
        assert listing["sessions"][0]["run_ids"] == [main.session.run_id]
        assert any(run["run_id"] == saved.run_id for run in listing["saved_runs"])
    finally:
        manager.close()
        manager.join()


def test_main_tools_coordinate_work_sessions_without_inheriting_control_tools(manager: WebSessions) -> None:
    main = ready(manager.get())
    expected = {"session_list", "session_start", "session_read", "session_send", "session_stop"}
    assert {extra.name for extra in main.session.plugins if extra.source == "main-session"} == expected
    main.session.ws.write("main-pending.py", b"not part of the host repository\n")
    started = tool(main, "session_start", name="Delegated", task="Inspect the repository")
    work_id = started["session_id"]
    work = ready(manager.get(work_id))
    assert work.session.first_task == "Inspect the repository"
    assert not work.session.plugins
    assert not (work.session.ws.current / "main-pending.py").exists()
    listing = tool(main, "session_list")
    assert listing["main_session_id"] == manager.main_id
    assert any(entry["id"] == work_id and entry["name"] == "Delegated" for entry in listing["sessions"])
    read = tool(main, "session_read", session_id=work_id, wait_seconds=5)
    assert read["id"] == work_id and read["status"] == "idle"
    work.session.llm = FakeLLM([reply(None, call("finish", summary="follow-up complete"))])
    assert tool(main, "session_send", session_id=work_id, task="Check one more thing")["session_id"] == work_id
    ready(work)
    assert work.session.instructions[-1] == "Check one more thing"
    assert tool(main, "session_stop", session_id=work_id)["stop_requested"] is False
    model = PausedModel("delegated.py")
    work.session.llm = model
    try:
        tool(main, "session_send", session_id=work_id, task="Add another file")
        assert model.entered.wait(5)
        assert tool(main, "session_stop", session_id=work_id)["stop_requested"] is True
        model.release.set()
        ready(work)
        assert work.session.last_result.reason == "cancelled"
    finally:
        model.release.set()


@pytest.mark.parametrize("instruction", ["/apply", " /quit", "/compare do something", "\n/stop"])
def test_main_tools_reject_slash_commands(manager: WebSessions, instruction: str) -> None:
    main = ready(manager.get())
    work_id = manager.create("Work")
    ready(manager.get(work_id))
    before = len(manager.listing()["sessions"])
    with pytest.raises(ValueError, match="ordinary task"):
        tool(main, "session_start", name="Invalid", task=instruction)
    with pytest.raises(ValueError, match="ordinary task"):
        tool(main, "session_send", session_id=work_id, task=instruction)
    assert len(manager.listing()["sessions"]) == before


def test_main_tools_reject_blank_followups_and_control_of_main(manager: WebSessions) -> None:
    main = ready(manager.get())
    work_id = manager.create("Work")
    with pytest.raises(ValueError, match="ordinary task"):
        tool(main, "session_send", session_id=work_id, task="  ")
    with pytest.raises(ValueError, match="Main"):
        tool(main, "session_send", session_id=manager.main_id, task="recursive work")
    with pytest.raises(ValueError, match="Main"):
        tool(main, "session_stop", session_id=manager.main_id)
    with pytest.raises(ValueError, match="itself"):
        tool(main, "session_read", session_id=manager.main_id, wait_seconds=1)


def test_http_session_list_create_and_rename(manager: WebSessions, server: str) -> None:
    status, listing = request(server, "/sessions")
    assert status == 200 and listing["main_session_id"] == manager.main_id
    status, created = request(server, "/sessions/new", {"name": "From browser"})
    assert status == 200
    work_id = created["session_id"]
    work = ready(manager.get(work_id))
    status, _ = request(server, "/sessions/rename", {"session_id": work_id, "name": "Renamed in browser"})
    assert status == 200 and work.state()["session_name"] == "Renamed in browser"
    status, listing = request(server, "/sessions")
    assert status == 200
    assert any(entry["id"] == work_id and entry["name"] == "Renamed in browser" for entry in listing["sessions"])


def test_shutdown_closes_every_worker(manager: WebSessions, server: str) -> None:
    main = ready(manager.get())
    work = ready(manager.get(manager.create("Work")))
    status, _ = request(server, "/shutdown", {})
    assert status == 200
    main.worker.join(5)
    work.worker.join(5)
    assert main.closed and work.closed and manager.closing
