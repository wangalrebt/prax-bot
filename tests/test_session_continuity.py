"""Persistent session recovery preserves evidence and serializes host changes."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from prax_bot.core.llm import Usage
from prax_bot.extensions.plugins import ExtraTool
from prax_bot.runtime.apply import ApplyTransaction, repository_apply_lock
from prax_bot.runtime.factory import create_session
from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import FakeLLM, call, reply
from tests.helpers import make_session


def test_continuation_restores_context_checks_pending_patch_and_accounting(tmp_path: Path) -> None:
    original = make_session(
        tmp_path,
        test_command="printf saved-check",
        llm=FakeLLM(
            [
                reply(None, call("write_file", path="pending.py", content="pending\n")),
                reply(None, call("finish", summary="ready to review")),
            ]
        ),
    )
    original.instruct("prepare a change")
    original.goal_info = {"objective": "keep working", "rounds": 1, "met": False}
    original.llm.usage = Usage(100, 20, 2, {"m": {"prompt_tokens": 100, "completion_tokens": 20, "calls": 2}})
    original.ws.total_cost = 1.25
    original.close()
    saved = load_run(original.run_id, original.settings)

    continued = create_session(
        original.settings,
        original.root,
        resume=(saved, len(saved.checkpoints) - 1),
        continue_session=True,
        llm=FakeLLM([reply(None, call("finish", summary="review complete"))] * 2),
    )
    try:
        state = continued.state()
        assert state["run_id"] == continued.run_id != original.run_id
        assert state["rounds"] == 1 and state["steps"] == saved.info["steps"]
        assert (state["tokens"], state["calls"], state["cost"]) == (120, 2, 1.25)
        assert continued.llm.usage.by_model == saved.info["usage_by_model"]
        assert state["files"] == ["pending.py"] and "pending" in state["diff"]
        assert state["summary"] == "ready to review" and not state["can_undo"]
        assert state["verification"] == "passed"
        assert continued.checks[0].output == "saved-check"
        assert continued.checks[0].image == continued.ws.image_id != saved.checkpoints[-1]["image"]
        assert continued.checks[0].baseline == original.checks[0].baseline
        assert continued.goal_info == original.goal_info
        assert continued._retired_commands == saved.info["sandbox_commands"]
        assert not (original.root / "pending.py").exists()
        continued.instruct("review it")
        assert continued.rounds == 2 and continued.llm.usage.calls == 4
        task = next(message["content"] for message in continued.llm.requests[0][0] if message["role"] == "user")
        assert "prepare a change" in task and "ready to review" in task and "review it" in task
    finally:
        continued.close()


def test_stale_local_check_cannot_match_a_reused_snapshot_name(tmp_path: Path) -> None:
    original = make_session(tmp_path, test_command="printf old-evidence")
    original.verify()
    old_check = original.checks[0]
    assert old_check.image == "local:snap-0"
    original.ws.write("later.py", b"not checked\n")
    original.close()
    saved = load_run(original.run_id, original.settings)

    continued = create_session(
        original.settings,
        original.root,
        resume=(saved, len(saved.checkpoints) - 1),
        continue_session=True,
        llm=FakeLLM([]),
    )
    try:
        assert continued.ws.image_id == old_check.image
        assert continued.verification_status == "stale"
        assert continued.checks[0].image == f"saved:{original.run_id}:{old_check.image}"
        assert continued.checks[0].output == old_check.output
    finally:
        continued.close()
    saved_again = load_run(continued.run_id, continued.settings)
    reopened = create_session(
        continued.settings,
        continued.root,
        resume=(saved_again, len(saved_again.checkpoints) - 1),
        continue_session=True,
        llm=FakeLLM([]),
    )
    try:
        assert reopened.verification_status == "stale"
        assert reopened.checks[0].image == continued.checks[0].image
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["test", "lint", "remove"])
def test_current_config_controls_required_checks_while_saved_goal_evidence_survives(
    tmp_path: Path, change: str
) -> None:
    original = make_session(tmp_path, test_command="true")
    original._goal_check = "printf goal-evidence"
    original.verify()
    original._goal_check = None
    original.close()
    saved = load_run(original.run_id, original.settings)
    settings = dataclasses.replace(
        original.settings,
        test_command="false" if change == "test" else None if change == "remove" else "true",
        lint_command="false" if change == "lint" else None,
    )
    continued = create_session(
        settings,
        original.root,
        resume=(saved, len(saved.checkpoints) - 1),
        continue_session=True,
        llm=FakeLLM([reply(None, call("finish", summary="continued"))] * 2),
    )
    try:
        assert continued.verification_status == ("passed" if change == "remove" else "incomplete")
        assert [(check.name, check.command) for check in continued.checks] == [
            ("tests", "true"),
            ("goal", "printf goal-evidence"),
        ]
        assert continued.checks[-1].output == "goal-evidence"
        assert continued._expected_checks == (*continued.check_commands, ("goal", "printf goal-evidence"))
        continued.instruct("continue with current checks")
        assert continued._expected_checks == continued.check_commands
        assert all(check.name != "goal" for check in continued.checks)
    finally:
        continued.close()


def test_pending_apply_recovery_keeps_check_logs_but_invalidates_their_freshness(tmp_path: Path) -> None:
    original = make_session(tmp_path, test_command="printf before-apply")
    original.ws.write("a.py", b"changed\n")
    original.ws.write("pending.py", b"still pending\n")
    original.verify()
    with (
        patch("prax_bot.runtime.apply.advance_baseline", side_effect=WorkspaceError("interrupted")),
        pytest.raises(WorkspaceError, match="interrupted"),
    ):
        original.apply(["a.py"])
    original.close()
    saved = load_run(original.run_id, original.settings)
    assert saved.pending_apply is not None

    continued = create_session(
        original.settings,
        original.root,
        resume=(saved, len(saved.checkpoints) - 1),
        continue_session=True,
        llm=FakeLLM([]),
    )
    try:
        assert continued.state()["files"] == ["pending.py"]
        assert continued.checks[0].output == "before-apply"
        assert continued.verification_status == "stale"
        assert (original.root / "a.py").read_text() == "changed\n"
        assert not (original.root / "pending.py").exists()
    finally:
        continued.close()


@pytest.mark.parametrize("recover_newer_children", [False, True])
def test_continuation_counts_parent_and_comparison_usage_once_across_reopens(
    tmp_path: Path, recover_newer_children: bool
) -> None:
    original = make_session(tmp_path)
    original.steps = 3
    original.rounds = 2
    original.applied_rounds = 1
    original.has_applied = True
    original.ws.total_cost = 1.5
    original.llm.usage = Usage(100, 20, 3, {"m": {"prompt_tokens": 100, "completion_tokens": 20, "calls": 3}})

    def model(settings):
        result = FakeLLM(
            [
                reply(None, call("write_file", path="candidate.py", content=current_thread().name)),
                reply(None, call("finish", summary=current_thread().name)),
            ]
        )
        result.usage = Usage(20, 5, 0, {"m": {"prompt_tokens": 20, "completion_tokens": 5, "calls": 2}})
        return result

    real_reopen = reopen_workspace

    def charged_workspace(*args, **kwargs):
        workspace = real_reopen(*args, **kwargs)
        workspace.total_cost = 2.0
        return workspace

    with (
        patch("prax_bot.core.llm.LLM", side_effect=model),
        patch("prax_bot.runtime.session.reopen_workspace", side_effect=charged_workspace),
    ):
        original.compare("compare approaches")
    original.close()
    previous = original
    expected_by_model = load_run(original.run_id, original.settings).info["usage_by_model"]
    for index in range(2):
        saved = load_run(previous.run_id, previous.settings)
        if recover_newer_children and index == 0:
            saved.info["comparisons"][0]["status"] = "running"
            for candidate in saved.info["comparisons"][0]["candidates"]:
                candidate["status"] = "working"
                for key in (
                    "steps",
                    "prompt_tokens",
                    "completion_tokens",
                    "model_calls",
                    "sandbox_commands",
                    "sandbox_cost",
                ):
                    saved.info[key] -= candidate[key]
                    candidate[key] = 0
                for model, counts in candidate["usage_by_model"].items():
                    for key, value in counts.items():
                        saved.info["usage_by_model"][model][key] -= value
                candidate["usage_by_model"] = {}
        continued = create_session(
            previous.settings,
            previous.root,
            resume=(saved, len(saved.checkpoints) - 1),
            continue_session=True,
            llm=FakeLLM([]),
        )
        try:
            assert continued.state()["tokens"] == 170
            assert continued.state()["calls"] == 7
            assert continued.state()["steps"] == 7
            assert continued.state()["cost"] == 5.5
            assert continued.llm.usage.total_tokens == 120
            assert continued.llm.usage.calls == 3 and continued.steps == 3
            assert continued._totals()["usage_by_model"] == expected_by_model
            assert continued.rounds == 2 and continued.applied_rounds == 1 and continued.has_applied
            assert continued.state()["comparison"]["id"] == original.comparisons[0]["id"]
            assert not continued.state()["comparison"]["stale"]
        finally:
            continued.close()
        previous = continued


def test_continuation_requires_a_saved_final_checkpoint_and_default_resume_stays_fresh(tmp_path: Path) -> None:
    original = make_session(tmp_path, test_command="true")
    original.ws.write("pending.py", b"pending\n")
    original.verify()
    original.rounds = 4
    original.llm.usage.add("m", 20, 5)
    original.close()
    saved = load_run(original.run_id, original.settings)
    with pytest.raises(ValueError, match="saved run"):
        create_session(original.settings, original.root, continue_session=True, llm=FakeLLM([]))
    with pytest.raises(ValueError, match="last checkpoint"):
        create_session(
            original.settings,
            original.root,
            resume=(saved, 0),
            continue_session=True,
            llm=FakeLLM([]),
        )
    resumed = create_session(
        original.settings, original.root, resume=(saved, len(saved.checkpoints) - 1), llm=FakeLLM([])
    )
    try:
        assert resumed.rounds == 0 and resumed.llm.usage.total_tokens == 0 and not resumed.checks
    finally:
        resumed.close()


def test_main_coordinator_tools_do_not_reach_comparison_workers(tmp_path: Path) -> None:
    class Arguments(BaseModel):
        pass

    called: list[str] = []
    original = make_session(tmp_path)
    original.plugins = (
        ExtraTool("main_only", "main", Arguments, lambda *_: called.append("main") or "main", source="main-session"),
        ExtraTool("ordinary", "regular plugin", Arguments, lambda *_: called.append("ordinary") or "ok"),
    )

    def model(settings):
        return FakeLLM(
            [
                reply(None, call("main_only")),
                reply(None, call("ordinary")),
                reply(None, call("finish", summary="finished")),
            ]
        )

    try:
        with patch("prax_bot.core.llm.LLM", side_effect=model):
            comparison = original.compare("try both")
        assert called == ["ordinary", "ordinary"]
        assert all(candidate["trajectory"][0]["error"] for candidate in comparison["candidates"])
        assert original.plugins[0].name == "main_only"
    finally:
        original.close()


@pytest.mark.parametrize("overlap", [False, True])
def test_concurrent_session_applies_lock_preparation_through_host_update(tmp_path: Path, overlap: bool) -> None:
    first = make_session(tmp_path)
    saved = load_run(first.run_id, first.settings)
    second = create_session(first.settings, first.root, resume=(saved, 0), llm=FakeLLM([]))
    first.ws.write("first.py", b"first\n")
    second.ws.write("first.py" if overlap else "second.py", b"second\n")
    first_prepared, second_prepared, release = Event(), Event(), Event()
    failures: list[BaseException] = []
    prepare = ApplyTransaction.prepare

    def paused_prepare(directory, root, changes):
        transaction = prepare(directory, root, changes)
        if directory == first.run_dir:
            first_prepared.set()
            assert release.wait(5), "test did not release the first transaction"
        else:
            second_prepared.set()
        return transaction

    def apply(session):
        try:
            session.apply()
        except BaseException as error:
            failures.append(error)

    workers = [Thread(target=apply, args=(session,)) for session in (first, second)]
    try:
        with patch.object(ApplyTransaction, "prepare", side_effect=paused_prepare):
            workers[0].start()
            assert first_prepared.wait(5)
            workers[1].start()
            assert not second_prepared.wait(0.15), "another apply prepared against an unfinished host transaction"
            release.set()
            for worker in workers:
                worker.join(10)
        assert not any(worker.is_alive() for worker in workers)
        assert (first.root / "first.py").read_text() == "first\n"
        if overlap:
            assert len(failures) == 1 and isinstance(failures[0], WorkspaceError)
            assert not (first.root / "second.py").exists()
        else:
            assert failures == []
            assert (first.root / "second.py").read_text() == "second\n"
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(10)
        first.close()
        second.close()


def test_pending_apply_recovery_uses_the_same_repository_lock(tmp_path: Path) -> None:
    original = make_session(tmp_path)
    original.ws.write("pending.py", b"pending\n")
    with (
        patch.object(original, "_finish_apply", side_effect=WorkspaceError("interrupted")),
        pytest.raises(WorkspaceError, match="interrupted"),
    ):
        original.apply()
    original.close()
    saved = load_run(original.run_id, original.settings)
    entered, finished = Event(), Event()
    failures: list[BaseException] = []
    finish = ApplyTransaction.finish

    def observed_finish(transaction, root, workspace):
        entered.set()
        return finish(transaction, root, workspace)

    def recover():
        try:
            reopen_workspace(saved, len(saved.checkpoints) - 1, original.settings, new_run_id="recovered")
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    worker = Thread(target=recover)
    try:
        with patch.object(ApplyTransaction, "finish", observed_finish):
            with repository_apply_lock(original.root):
                worker.start()
                assert entered.wait(5)
                assert not finished.wait(0.15), "restart recovery bypassed the active repository transaction"
                assert not (original.root / "pending.py").exists()
            worker.join(10)
        assert not worker.is_alive() and failures == []
        assert (original.root / "pending.py").read_text() == "pending\n"
    finally:
        worker.join(10)
