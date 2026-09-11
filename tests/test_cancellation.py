"""Stopping waits for an in-flight call and persists completed work before closing."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

from prax_bot.ui.web import WebSession
from tests.fakes import FakeLLM, call, reply
from tests.helpers import make_session, settings


class PausedModel(FakeLLM):
    def __init__(self):
        super().__init__(
            [
                reply(None, call("write_file", path="b.py", content="kept\n")),
                reply(None, call("write_file", path="c.py", content="must not run\n")),
            ]
        )
        self.entered, self.release = Event(), Event()

    def complete(self, *args, **kwargs):
        if len(self.requests) == 1:
            self.entered.set()
            assert self.release.wait(5), "test did not release the model call"
        return super().complete(*args, **kwargs)


def test_stop_preserves_completed_edits_and_skips_the_next_tool(tmp_path: Path) -> None:
    model = PausedModel()
    session = make_session(tmp_path, llm=model)
    worker = Thread(target=lambda: session.instruct("add files"))
    try:
        worker.start()
        assert model.entered.wait(5)
        session.request_stop()
        assert worker.is_alive() and not session.events_file.closed
        model.release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert session.last_result.reason == "cancelled" and session.last_result.steps == 1
        assert session.ws.read("b.py") == b"kept\n"
        assert not (session.ws.current / "c.py").exists()
        record = json.loads((session.run_dir / "run.json").read_text())
        assert record["reason"] == "cancelled" and record["steps"] == 1
        assert "b.py" in (session.run_dir / "diff.patch").read_text()
    finally:
        model.release.set()
        worker.join(5)
        session.close()


def test_web_close_waits_for_the_worker_before_closing_its_log(tmp_path: Path) -> None:
    model = PausedModel()
    web = WebSession(settings(tmp_path), tmp_path, "local", lambda out, hook: make_session(tmp_path, llm=model))
    try:
        web.submit("add files")
        assert model.entered.wait(5)
        web.close()
        assert not web.closed and not web.session.events_file.closed
        model.release.set()
        web.worker.join(5)
        assert web.closed and web.session.events_file.closed
        assert web.session.last_result.reason == "cancelled"
        assert "b.py" in (web.session.run_dir / "diff.patch").read_text()
    finally:
        model.release.set()
        web.close()
        web.worker.join(5)
