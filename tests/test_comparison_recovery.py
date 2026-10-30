"""Recovery, cancellation, and accounting evidence for independent comparison candidates."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

import pytest

from prax_bot.runtime.factory import create_session
from prax_bot.runtime.runs import load_run, local_snapshot, new_run, reopen_workspace, run_status
from prax_bot.runtime.session import Session
from prax_bot.workspace.sandbox import SandboxWorkspace
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import FakeLLM, base, call, fake_client, reply
from tests.helpers import git_repo, make_session, settings


def candidate_model(settings):
    label = current_thread().name.rsplit("-", 1)[-1]
    model = FakeLLM(
        [
            reply(None, call("write_file", path="candidate.py", content=label + "\n")),
            reply(None, call("finish", summary=f"solution {label}")),
        ]
    )
    model.usage.prompt_tokens = 20
    model.usage.completion_tokens = 5
    return model


def test_stop_preserves_both_candidates_edits_and_stops_both_active_models(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    entered = {label: Event() for label in ("A", "B")}
    release = Event()
    errors: list[BaseException] = []

    def model(settings):
        label = current_thread().name.rsplit("-", 1)[-1]
        instance = candidate_model(settings)
        complete = instance.complete

        def wait_after_edit(*args, **kwargs):
            if instance.requests:
                entered[label].set()
                assert release.wait(10), "test did not release the current model calls"
            return complete(*args, **kwargs)

        instance.complete = wait_after_edit
        return instance

    def compare():
        try:
            session.compare("fix the issue")
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=compare)
    try:
        with patch("prax_bot.core.llm.LLM", side_effect=model):
            worker.start()
            assert entered["A"].wait(5) and entered["B"].wait(5)
            session.request_stop()
            release.set()
            worker.join(15)
        assert not worker.is_alive() and errors == []
        assert not session.active and not session.cancel.is_set()
        comparison = session.state()["comparison"]
        assert comparison["status"] == "stopped"
        assert not (session.ws.current / "candidate.py").exists()
        for candidate in comparison["candidates"]:
            assert candidate["status"] == "stopped"
            saved = load_run(candidate["run_id"], session.settings)
            assert saved.info["reason"] == "cancelled" and run_status(saved).live is False
            snapshot = local_snapshot(saved, candidate["image"], session.settings)
            assert (snapshot / "candidate.py").read_text() == candidate["label"] + "\n"
            assert candidate["trajectory"][0]["name"] == "write_file" and candidate["diff"]
    finally:
        release.set()
        worker.join(15)
        session.close()


def test_one_candidate_startup_failure_keeps_the_other_candidate_reviewable(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    original_reopen = reopen_workspace

    def open_candidate(*args, **kwargs):
        if current_thread().name.endswith("-B"):
            raise WorkspaceError("candidate B could not start")
        return original_reopen(*args, **kwargs)

    try:
        with (
            patch("prax_bot.runtime.session.reopen_workspace", side_effect=open_candidate),
            patch("prax_bot.core.llm.LLM", side_effect=candidate_model),
        ):
            comparison = session.compare("fix the issue")
        candidates = {item["label"]: item for item in comparison["candidates"]}
        assert candidates["A"]["status"] == "complete" and candidates["A"]["diff"]
        assert candidates["B"]["status"] == "error" and "could not start" in candidates["B"]["error"]
        assert run_status(load_run(candidates["A"]["run_id"], session.settings)).live is False
        assert run_status(load_run(candidates["B"]["run_id"], session.settings)).live is False
        with pytest.raises(ValueError, match="completed candidate"):
            session.choose_candidate("B")
        session.choose_candidate("A")
        assert session.ws.read("candidate.py") == b"A\n" and not (session.root / "candidate.py").exists()
    finally:
        session.close()


def test_parent_and_candidate_costs_are_counted_once_after_selection(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    session.ws.total_cost = 3.0
    original_reopen = reopen_workspace

    def open_with_cost(*args, **kwargs):
        workspace = original_reopen(*args, **kwargs)
        # Simulate costs already incurred by the backend while opening each workspace.
        label = current_thread().name.rsplit("-", 1)[-1]
        workspace.total_cost = {"A": 2.0, "B": 5.0}.get(label, 0.75)
        return workspace

    try:
        with (
            patch("prax_bot.runtime.session.reopen_workspace", side_effect=open_with_cost),
            patch("prax_bot.core.llm.LLM", side_effect=candidate_model),
        ):
            result = session.compare("fix the issue")
            before = load_run(session.run_id, session.settings).info
            assert before["sandbox_cost"] == 10.0
            assert [candidate["cost"] for candidate in result["candidates"]] == [2.0, 5.0]
            assert session.state()["tokens"] == 50 and session.state()["calls"] == 4
            session.choose_candidate("B")
        after = load_run(session.run_id, session.settings).info
        assert after["sandbox_cost"] == 10.75 and session.state()["cost"] == 10.75
        assert after["sandbox_commands"] == before["sandbox_commands"] + session.ws.commands
        assert session.state()["tokens"] == 50 and session.state()["calls"] == 4
        assert session.state()["comparison"]["selected"] == "B" and not session.state()["comparison"]["stale"]
    finally:
        session.close()


def test_resume_without_comparisons_keeps_current_session_candidate_usage_and_history(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    unrelated = create_session(
        session.settings,
        session.root,
        resume=(load_run(session.run_id, session.settings), 0),
        llm=FakeLLM([]),
    )
    unrelated.close()
    try:
        with patch("prax_bot.core.llm.LLM", side_effect=candidate_model):
            comparison = session.compare("fix the issue")
        before = session.state()
        session.resume(unrelated.run_id)
        after = session.state()
        assert (after["tokens"], after["calls"], after["steps"]) == (before["tokens"], before["calls"], before["steps"])
        assert after["comparison"]["id"] == comparison["id"] and after["comparison"]["stale"]
        saved = load_run(session.run_id, session.settings)
        assert [item["id"] for item in saved.info["comparisons"]] == [comparison["id"]]
        with pytest.raises(ValueError, match="workspace changed"):
            session.choose_candidate("A")
    finally:
        session.close()


def test_resume_recovers_closed_child_evidence_from_an_interrupted_parent_record(tmp_path: Path) -> None:
    original = make_session(tmp_path, test_command="test -f candidate.py")
    with patch("prax_bot.core.llm.LLM", side_effect=candidate_model):
        completed = original.compare("fix the issue")
    original.close()
    saved = load_run(original.run_id, original.settings)
    saved_comparison = saved.info["comparisons"][-1]
    saved_comparison["status"] = "running"
    for candidate in saved_comparison["candidates"]:
        candidate.update(status="working", summary="", checks=[], diff="", trajectory=[], tokens=0, calls=0)
    (saved.directory / "run.json").write_text(json.dumps(saved.info))
    saved = load_run(original.run_id, original.settings)

    with patch("prax_bot.core.llm.LLM", side_effect=AssertionError("recovery must not start models")):
        session = create_session(
            original.settings, original.root, resume=(saved, len(saved.checkpoints) - 1), llm=FakeLLM([])
        )
        try:
            comparison = session.state()["comparison"]
            assert comparison["status"] == "interrupted" and not comparison["stale"]
            for candidate, expected in zip(comparison["candidates"], completed["candidates"], strict=True):
                assert candidate["status"] == "complete" and candidate["summary"] == expected["summary"]
                assert candidate["diff"] == expected["diff"]
                assert candidate["checks"] == json.loads(json.dumps(expected["checks"]))
                assert candidate["tokens"] == expected["tokens"] and candidate["calls"] == expected["calls"]
                assert candidate["trajectory"] == expected["trajectory"] and candidate["verification"] == "passed"
            session.choose_candidate("A", comparison_id=comparison["id"])
            assert session.verification_status == "passed" and session.ws.read("candidate.py") == b"A\n"
        finally:
            session.close()


@pytest.mark.parametrize("mismatch", ["repository", "checkpoint", "patch"])
def test_selection_rejects_changed_candidate_records_or_snapshots(tmp_path: Path, mismatch: str) -> None:
    session = make_session(tmp_path)
    try:
        with patch("prax_bot.core.llm.LLM", side_effect=candidate_model):
            comparison = session.compare("fix the issue")
        candidate = comparison["candidates"][0]
        saved = load_run(candidate["run_id"], session.settings)
        if mismatch == "patch":
            snapshot = local_snapshot(saved, candidate["image"], session.settings)
            (snapshot / "candidate.py").write_text("changed after comparison\n")
        else:
            if mismatch == "repository":
                saved.info["repo"] = str(tmp_path / "another-repository")
            else:
                saved.info["checkpoints"][-1]["image"] = "local:snap-999"
            (saved.directory / "run.json").write_text(json.dumps(saved.info))
        workspace = session.ws
        with pytest.raises(ValueError, match=r"saved candidate.*changed"):
            session.choose_candidate("A")
        assert session.ws is workspace and session.state()["comparison"]["selected"] is None
        assert not (session.root / "candidate.py").exists()
    finally:
        session.close()


def test_contree_candidates_fork_one_image_and_keep_edits_separate(tmp_path: Path) -> None:
    config = settings(tmp_path)
    repo = git_repo(tmp_path / "repo")
    log: list = []
    client = fake_client(log)
    workspace = SandboxWorkspace(client, base(log), command_timeout=5)
    workspace.mark_clean()
    workspace.write("inherited.py", b"pending\n")
    source_image = workspace.image_id
    run_id, directory, events, emit = new_run(config)
    session = Session(
        config,
        workspace,
        repo,
        backend="contree",
        run_id=run_id,
        run_dir=directory,
        events_file=events,
        on_event=emit,
        llm=FakeLLM([]),
    )
    try:
        with (
            patch("prax_bot.workspace.sandbox.make_client", return_value=client),
            patch("prax_bot.core.llm.LLM", side_effect=candidate_model),
        ):
            comparison = session.compare("fix the issue")
            assert session.ws is workspace and workspace.image_id == source_image
            with pytest.raises(FileNotFoundError):
                workspace.read("candidate.py")
            for candidate in comparison["candidates"]:
                assert candidate["status"] == "complete"
                saved = load_run(candidate["run_id"], config)
                assert saved.checkpoints[0]["image"] == source_image
                image = client.images.use(candidate["image"])
                assert image.read("/work/candidate.py") == (candidate["label"] + "\n").encode()
                assert image.read("/work/inherited.py") == b"pending\n"
            session.choose_candidate("B")
            assert session.ws.read("candidate.py") == b"B\n" and session.ws.read("inherited.py") == b"pending\n"
            assert workspace.image_id == source_image and not (repo / "candidate.py").exists()
    finally:
        session.close()
