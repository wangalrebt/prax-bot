"""Candidate attempts must stay isolated, reviewable, cancellable, and explicitly selected."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

import pytest

from prax_bot.runtime.factory import create_session
from prax_bot.runtime.runs import load_run, reopen_workspace, run_status
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import FakeLLM, call, reply
from tests.helpers import make_session


def candidate_model(settings):
    label = current_thread().name.rsplit("-", 1)[-1]
    model = FakeLLM(
        [
            reply(None, call("read_file", path="inherited.py")),
            reply(None, call("write_file", path="candidate.txt", content=label + "\n")),
            reply(None, call("finish", summary=f"solution {label}")),
        ]
    )
    model.usage.prompt_tokens = 100
    model.usage.completion_tokens = 10
    return model


def compare(session):
    session.ws.write("inherited.py", b"pending before comparison\n")
    with patch("prax_bot.core.llm.LLM", side_effect=candidate_model):
        return session.compare("fix the issue", approach_a="small change", approach_b="different implementation")


def test_comparison_forks_the_same_pending_state_and_choosing_does_not_apply(tmp_path: Path) -> None:
    session = make_session(tmp_path, test_command="test -f inherited.py && test -f candidate.txt")
    session.ws.mark_clean()
    try:
        workspace = session.ws
        result = compare(session)
        assert session.ws is workspace
        assert session.ws.changes().files == ("inherited.py",)
        assert result["selected"] is None and result["status"] == "complete"
        assert result["stale"] is False
        assert not (session.root / "candidate.txt").exists()
        for candidate in result["candidates"]:
            assert candidate["status"] == "complete" and candidate["verification"] == "passed"
            assert "pending before comparison" in candidate["diff"]
            assert candidate["checks"][0]["baseline"]["passed"] is False
            assert candidate["checks"][0]["passed"] is True
            assert "pending before comparison" in candidate["trajectory"][0]["result"]
            saved = load_run(candidate["run_id"], session.settings)
            assert run_status(saved).live is False
            assert saved.info["resumed_from"]["image"] == result["source"]["image"]
            assert saved.diff == candidate["diff"]
        before = session.state()
        assert before["tokens"] == 220 and before["calls"] == 6 and before["steps"] == 6
        session.choose_candidate("B", comparison_id=result["id"])
        assert session.ws.read("candidate.txt") == b"B\n"
        assert session.ws.read("inherited.py") == b"pending before comparison\n"
        assert session.verification_status == "passed" and not session.state()["can_undo"]
        assert session.checks[0].image == session.ws.image_id
        assert not (session.root / "candidate.txt").exists()
        after = session.state()
        assert (after["tokens"], after["calls"], after["steps"]) == (220, 6, 6)
        with pytest.raises(ValueError, match="already been chosen"):
            session.choose_candidate("A")
        assert session.apply()
        assert (session.root / "candidate.txt").read_text() == "B\n"
        assert (session.root / "inherited.py").read_text() == "pending before comparison\n"
        saved = load_run(session.run_id, session.settings)
        assert saved.info["model_calls"] == 6 and saved.info["prompt_tokens"] == 200
        assert len(saved.info["comparisons"][0]["candidates"]) == 2
    finally:
        session.close()


def test_comparison_candidates_keep_no_edit_confirmation_despite_coordinating_parent(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    session.confirm_no_edit_finish = False

    def model(settings):
        return FakeLLM(
            [
                reply(None, call("finish", summary="No edits")),
                reply(None, call("finish", summary="Confirmed read-only result")),
            ]
        )

    try:
        with patch("prax_bot.core.llm.LLM", side_effect=model):
            result = session.compare("Inspect the repository without changing files")
        for candidate in result["candidates"]:
            assert candidate["status"] == "complete" and candidate["calls"] == 2
            assert candidate["summary"] == "Confirmed read-only result"
            assert candidate["trajectory"][0]["error"] is True
            assert "no changes have been made" in candidate["trajectory"][0]["result"]
    finally:
        session.close()


@pytest.mark.parametrize("mutation", ["write", "same_image_other_workspace", "apply"])
def test_selection_rejects_changed_parent_state(tmp_path: Path, mutation: str) -> None:
    session = make_session(tmp_path)
    try:
        result = compare(session)
        if mutation == "write":
            session.ws.write("later.py", b"later\n")
        elif mutation == "apply":
            session.apply()
        else:
            # Local image ids collide between runs; resume must change workspace identity too.
            session.resume(result["candidates"][0]["run_id"])
            # Keep the old comparison visible, exactly as a retained review would be.
            session.comparisons = [result]
        with pytest.raises(ValueError, match="workspace changed"):
            session.choose_candidate("A")
    finally:
        session.close()


def test_completed_comparison_survives_resume_without_running_agents(tmp_path: Path) -> None:
    original = make_session(tmp_path, test_command="true")
    result = compare(original)
    original.close()
    saved = load_run(original.run_id, original.settings)
    with patch("prax_bot.core.llm.LLM", side_effect=AssertionError("must not run models")):
        session = create_session(
            original.settings, original.root, resume=(saved, len(saved.checkpoints) - 1), llm=FakeLLM([])
        )
        try:
            assert not session.state()["comparison"]["stale"]
            assert session.state()["comparison"]["id"] == result["id"]
            session.choose_candidate("A")
            assert session.ws.read("candidate.txt") == b"A\n"
            assert session.state()["tokens"] == 220
            assert session.verification_status == "passed"
        finally:
            session.close()


def test_interrupted_comparison_is_retained_without_restarting(tmp_path: Path) -> None:
    original = make_session(tmp_path)
    compare(original)
    original.comparisons[-1]["status"] = "running"
    original.comparisons[-1]["candidates"][1]["status"] = "working"
    unfinished = load_run(original.comparisons[-1]["candidates"][1]["run_id"], original.settings)
    unfinished.info.update(state="working", finished=False)
    (unfinished.directory / "run.json").write_text(json.dumps(unfinished.info))
    original.close()
    saved = load_run(original.run_id, original.settings)
    session = create_session(
        original.settings, original.root, resume=(saved, len(saved.checkpoints) - 1), llm=FakeLLM([])
    )
    try:
        restored = session.state()["comparison"]
        assert restored["status"] == "interrupted"
        assert restored["candidates"][1]["status"] == "interrupted"
        assert restored["candidates"][0]["status"] == "complete"
        session.choose_candidate("A")
    finally:
        session.close()


def test_stop_during_initialization_also_stops_the_late_candidate(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    entered, release = Event(), Event()
    original_reopen = reopen_workspace
    models = []

    def delayed(*args, **kwargs):
        if current_thread().name.endswith("B"):
            entered.set()
            assert release.wait(5)
        return original_reopen(*args, **kwargs)

    def model(settings):
        instance = candidate_model(settings)
        models.append(instance)
        return instance

    worker = Thread(target=lambda: session.compare("fix it"))
    try:
        with (
            patch("prax_bot.runtime.session.reopen_workspace", side_effect=delayed),
            patch("prax_bot.core.llm.LLM", side_effect=model),
        ):
            worker.start()
            assert entered.wait(5)
            assert session.active
            with pytest.raises(RuntimeError, match="already working"):
                session.undo()
            session.request_stop()
            release.set()
            worker.join(10)
        assert not worker.is_alive() and not session.active
        result = session.state()["comparison"]
        assert result["status"] == "stopped" and result["candidates"][1]["status"] == "stopped"
        assert not models[-1].requests
        assert session.cancel.is_set() is False
    finally:
        release.set()
        worker.join(10)
        session.close()


def test_comparison_refuses_pending_apply_and_wrong_choice_id(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        result = compare(session)
        with pytest.raises(ValueError, match="no longer current"):
            session.choose_candidate("A", comparison_id="old")
        with (
            patch("prax_bot.runtime.apply.advance_baseline", side_effect=WorkspaceError("record failed")),
            pytest.raises(WorkspaceError),
        ):
            session.apply()
        with (
            patch("prax_bot.runtime.session.reopen_workspace", side_effect=AssertionError("must not fork")),
            pytest.raises(WorkspaceError, match="apply is unfinished"),
        ):
            session.compare("more")
        assert session.comparisons[-1]["id"] == result["id"]
        session.apply()
    finally:
        session.close()


def test_second_comparison_keeps_both_attempts_in_usage_and_saved_history(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        compare(session)
        compare(session)
        assert session.state()["tokens"] == 440 and session.state()["calls"] == 12
        saved = json.loads((session.run_dir / "run.json").read_text())
        assert len(saved["comparisons"]) == 2 and saved["model_calls"] == 12
    finally:
        session.close()


def test_resuming_an_interrupted_apply_does_not_revalidate_an_old_comparison(tmp_path: Path) -> None:
    original = make_session(tmp_path)
    compare(original)
    with (
        patch("prax_bot.runtime.apply.advance_baseline", side_effect=WorkspaceError("record failed")),
        pytest.raises(WorkspaceError),
    ):
        original.apply()
    original.close()
    saved = load_run(original.run_id, original.settings)
    session = create_session(
        original.settings, original.root, resume=(saved, len(saved.checkpoints) - 1), llm=FakeLLM([])
    )
    try:
        assert session.state()["comparison"]["stale"]
        assert session.diff() == ""
        with pytest.raises(ValueError, match="workspace changed"):
            session.choose_candidate("A")
    finally:
        session.close()
