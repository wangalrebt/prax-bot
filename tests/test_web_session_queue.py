"""Queue transitions stay atomic while submitting, stopping and closing web sessions."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from prax_bot.ui.web import WebSession
from tests.helpers import settings


class SavedSession:
    """Record the callback/thread contract without starting a model or workspace."""

    def __init__(self, hook) -> None:
        self.run_id = "run-one"
        self.cancel = Event()
        self.closed = False
        self.hook = hook
        self.saved: list[tuple[str, dict[str, Any], str]] = []

    def state(self):
        return {"run_id": self.run_id, "stopping": self.cancel.is_set()}

    def request_stop(self) -> None:
        self.cancel.set()
        self.on_event("session.stop_requested")

    def on_event(self, kind: str, **payload: Any) -> None:
        assert not self.closed, "the event log is already closed"
        self.saved.append((kind, payload, current_thread().name))
        self.hook(kind, **payload)

    def close(self) -> None:
        self.closed = True


def test_submit_cannot_race_the_workers_empty_queue_and_idle_transition(tmp_path: Path, monkeypatch) -> None:
    observed_empty, release_empty = Event(), Event()
    second_started, release_second, submitted = Event(), Event(), Event()

    def execute(session, line, out):
        if line == "second":
            second_started.set()
            assert release_second.wait(5)
        return True

    monkeypatch.setattr("prax_bot.ui.web.handle_line", execute)
    web = WebSession(settings(tmp_path), tmp_path, "local", lambda out, hook: SavedSession(hook))
    assert web.idle.wait(5)
    empty = web.pending.empty
    paused = False

    def observed_queue_empty():
        nonlocal paused
        result = empty()
        if result and not paused:
            paused = True
            observed_empty.set()
            assert release_empty.wait(5)
        return result

    monkeypatch.setattr(web.pending, "empty", observed_queue_empty)

    def submit_second():
        web.submit("second")
        submitted.set()

    producer = Thread(target=submit_second)
    try:
        web.submit("first")
        assert observed_empty.wait(5)
        producer.start()
        assert not submitted.wait(0.1), "enqueue interleaved between the empty-queue check and idle signal"
        release_empty.set()
        assert submitted.wait(5) and second_started.wait(5)
        assert web.busy and not web.idle.is_set()
        release_second.set()
        assert web.idle.wait(5) and not web.busy
    finally:
        release_empty.set()
        release_second.set()
        if producer.ident is not None:
            producer.join(5)
        web.close()
        web.worker.join(5)
        assert not web.worker.is_alive()


def test_stop_cancels_queued_instructions_and_persists_them_on_the_worker(tmp_path: Path, monkeypatch) -> None:
    running, release = Event(), Event()
    executed: list[str] = []

    def execute(session, line, out):
        executed.append(line)
        if line == "first":
            running.set()
            assert release.wait(5)
        return True

    monkeypatch.setattr("prax_bot.ui.web.handle_line", execute)
    web = WebSession(settings(tmp_path), tmp_path, "local", lambda out, hook: SavedSession(hook))
    try:
        assert web.idle.wait(5)
        assert web.stop() is False
        web.submit("first")
        assert running.wait(5)
        web.submit("second", source_run="run-one")
        web.submit("third", source_run="run-one")
        assert web.stop() is True
        assert web.pending.empty() and web.session.cancel.is_set()
        assert [event["text"] for event in web.history if event["kind"] == "instruction.cancelled"] == [
            "second",
            "third",
        ]
        release.set()
        assert web.idle.wait(5)
        assert executed == ["first"] and not web.session.cancel.is_set()
        cancellations = [event for event in web.session.saved if event[0] == "web.instruction.cancelled"]
        assert [event[1]["text"] for event in cancellations] == ["second", "third"]
        assert all(event[2] == "praxbot-web-worker" for event in cancellations)
        assert not any(event["kind"] == "web.instruction.cancelled" for event in web.history), "no duplicate live logs"
        web.submit("after stop")
        assert web.idle.wait(5)
        assert executed == ["first", "after stop"]
    finally:
        release.set()
        web.close()
        web.worker.join(5)


def test_stop_cancels_waiting_work_during_startup_but_accepts_later_instructions(tmp_path: Path, monkeypatch) -> None:
    starting, release = Event(), Event()
    executed: list[str] = []

    def create(out, hook):
        starting.set()
        assert release.wait(5)
        return SavedSession(hook)

    monkeypatch.setattr("prax_bot.ui.web.handle_line", lambda session, line, out: executed.append(line) or True)
    web = WebSession(settings(tmp_path), tmp_path, "local", create)
    try:
        assert starting.wait(5)
        web.submit("before startup finishes")
        assert web.stop() is True
        assert web.pending.empty()
        assert any(event.get("text") == "before startup finishes" for event in web.history)
        web.submit("after stop")
        release.set()
        assert web.idle.wait(5)
        assert executed == ["after stop"]
        assert [(kind, payload["text"]) for kind, payload, _ in web.session.saved] == [
            ("web.instruction.cancelled", "before startup finishes"),
            ("web.instruction.start", "after stop"),
            ("web.instruction.done", "after stop"),
        ]
    finally:
        release.set()
        web.close()
        web.worker.join(5)


def test_closing_startup_preserves_cancellations_and_stop_cannot_consume_its_sentinel(tmp_path: Path) -> None:
    starting, release = Event(), Event()

    def create(out, hook):
        starting.set()
        assert release.wait(5)
        return SavedSession(hook)

    web = WebSession(settings(tmp_path), tmp_path, "local", create)
    try:
        assert starting.wait(5)
        web.submit("cancel on close")
        web.close()
        assert web.stop() is False
        with pytest.raises(RuntimeError, match="closing or closed"):
            web.submit("too late")
        release.set()
        web.worker.join(5)
        assert not web.worker.is_alive() and web.closed and web.session.closed
        assert web.pending.empty()
        assert any(kind == "web.instruction.cancelled" for kind, _, _ in web.session.saved)
        with pytest.raises(RuntimeError, match="closing or closed"):
            web.submit("/stop")
    finally:
        release.set()
        web.close()
        web.worker.join(5)


def test_close_stops_current_work_and_discards_waiting_instructions(tmp_path: Path, monkeypatch) -> None:
    running, release = Event(), Event()
    executed: list[str] = []

    def execute(session, line, out):
        executed.append(line)
        running.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr("prax_bot.ui.web.handle_line", execute)
    web = WebSession(settings(tmp_path), tmp_path, "local", lambda out, hook: SavedSession(hook))
    try:
        assert web.idle.wait(5)
        web.submit("running")
        assert running.wait(5)
        web.submit("waiting")
        web.close()
        assert web.session.cancel.is_set()
        assert web.stop() is True
        release.set()
        web.worker.join(5)
        assert not web.worker.is_alive() and executed == ["running"]
        assert web.pending.empty()
        assert any(
            kind == "web.instruction.cancelled" and payload["text"] == "waiting"
            for kind, payload, _ in web.session.saved
        )
    finally:
        release.set()
        web.close()
        web.worker.join(5)
