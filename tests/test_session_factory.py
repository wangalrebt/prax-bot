"""Session startup and shutdown always release the event writer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from prax_bot.runtime.factory import create_session
from prax_bot.runtime.runs import load_run, new_run, run_status
from tests.fakes import FakeLLM, call, reply
from tests.helpers import make_session


@pytest.mark.parametrize("failure", ["workspace", "model", "record"])
def test_failed_startup_closes_the_event_log(tmp_path: Path, failure: str) -> None:
    prepared = make_session(tmp_path)
    prepared.close()
    opened = []

    def log(settings, on_event):
        result = new_run(settings, on_event)
        opened.append(result[2])
        return result

    target = {
        "workspace": "prax_bot.runtime.factory.open_workspace",
        "model": "prax_bot.core.llm.LLM",
        "record": "prax_bot.runtime.session.write_record",
    }[failure]
    with (
        patch("prax_bot.runtime.factory.new_run", side_effect=log),
        patch("prax_bot.runtime.factory.open_workspace", return_value=prepared.ws),
        patch(target, side_effect=RuntimeError("startup failed")),
        pytest.raises(RuntimeError, match="startup failed"),
    ):
        create_session(prepared.settings, prepared.root, llm=None if failure == "model" else FakeLLM([]))
    assert len(opened) == 1 and opened[0].closed


def test_factory_persists_and_observes_each_event_once(tmp_path: Path) -> None:
    prepared = make_session(tmp_path, test_command="true", lint_command="true", context_budget=24000)
    prepared.close()
    observed = []
    with patch("prax_bot.runtime.factory.open_workspace", return_value=prepared.ws):
        session = create_session(
            prepared.settings,
            prepared.root,
            on_event=lambda kind, **payload: observed.append((kind, payload)),
            protect=("protected/*",),
            llm=FakeLLM([]),
        )
    try:
        session.ws.write("new.py", b"new\n")
        saved = load_run(session.run_id, session.settings)
        assert [kind for kind, _ in observed] == [event["kind"] for event in saved.events]
        assert sum(kind == "checkpoint" for kind, _ in observed) == 1
        assert session.settings.context_budget == 24000 and session.protect == ("protected/*",)
        assert session.check_commands == (("tests", "true"), ("lint", "true"))
        assert run_status(saved).live is True
    finally:
        session.close()
    assert run_status(load_run(session.run_id, session.settings)).live is False


def test_record_failure_during_close_still_releases_the_event_log(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    with patch.object(session, "record", side_effect=OSError("disk unavailable")), pytest.raises(OSError):
        session.close()
    assert session.closed and session.events_file.closed


def test_factory_resumes_the_workspace_and_continuation_context(tmp_path: Path) -> None:
    prepared = make_session(
        tmp_path,
        llm=FakeLLM(
            [
                reply(None, call("write_file", path="b.py", content="b\n")),
                reply(None, call("finish", summary="added b")),
            ]
        ),
    )
    try:
        prepared.instruct("add b")
    finally:
        prepared.close()
    saved = load_run(prepared.run_id, prepared.settings)
    model = FakeLLM(
        [
            reply(None, call("write_file", path="c.py", content="c\n")),
            reply(None, call("finish", summary="added c")),
        ]
    )
    session = create_session(
        prepared.settings, prepared.root, resume=(saved, len(saved.checkpoints) - 1), llm=model, kind="resume"
    )
    try:
        session.instruct("also add c")
        assert set(session.changes.files) == {"b.py", "c.py"}
        assert session.resumed_from["run_id"] == prepared.run_id
        assert session.first_task == "add b" and session.instructions == ["also add c"]
        task = next(m["content"] for m in model.requests[0][0] if m["role"] == "user")
        assert "added b" in task and "also add c" in task
    finally:
        session.close()
