"""Applying, resuming and changing settings must preserve the intended workspace state."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.workspace.repository import apply_diff
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import FakeLLM, call, reply
from tests.helpers import git_repo, make_run, make_session, make_snapshot


@pytest.mark.parametrize("after_host_apply", [False, True])
def test_apply_recovers_a_process_interruption_before_or_after_host_mutation(
    tmp_path: Path, after_host_apply: bool
) -> None:
    session = make_session(tmp_path)
    session.ws.write("a.py", b"changed\n")
    session.ws.write("test_new.py", b"pending\n")

    def interrupt(root, patch_text):
        if root == session.root:
            if after_host_apply:
                apply_diff(root, patch_text)
            raise SystemExit("interrupted")
        apply_diff(root, patch_text)

    try:
        with patch("prax_bot.runtime.apply.apply_diff", side_effect=interrupt), pytest.raises(SystemExit):
            session.apply(["a.py"])
        journal = json.loads((session.run_dir / "apply.json").read_text())
        assert journal["changes"]["files"] == ["a.py"] and not journal["host_applied"]
        # Resume only from persisted state, without relying on Session.close to save it.
        saved = load_run(session.run_id, session.settings)
        workspace = reopen_workspace(saved, len(saved.checkpoints) - 1, session.settings, new_run_id="recovered")
        assert (session.root / "a.py").read_text() == "changed\n"
        assert workspace.changes().files == ("test_new.py",)
        assert not (session.root / "test_new.py").exists()
    finally:
        session.close()


def test_recovery_preserves_edits_made_after_an_interrupted_apply(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"changed\n")

        def interrupt(root, patch_text):
            apply_diff(root, patch_text)
            if root == session.root:
                raise SystemExit("interrupted")

        with patch("prax_bot.runtime.apply.apply_diff", side_effect=interrupt), pytest.raises(SystemExit):
            session.apply()
        (session.root / "a.py").write_text("later user edit\n")
        with pytest.raises(WorkspaceError, match="files changed during an unfinished apply"):
            session.apply()
        assert (session.root / "a.py").read_text() == "later user edit\n"
        assert (session.run_dir / "apply.json").is_file()
        (session.root / "a.py").write_text("changed\n")
        assert session.apply() and session.diff() == ""
    finally:
        session.close()


def test_failed_diff_refresh_cannot_apply_an_old_patch(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"old patch\n")
        session.diff()
        session.ws.write("a.py", b"new patch\n")
        with (
            patch.object(session.ws, "changes", side_effect=WorkspaceError("read unavailable")),
            pytest.raises(WorkspaceError, match="read unavailable"),
        ):
            session.apply()
        assert (session.root / "a.py").read_text() == "x\n"
        assert session.last_diff == "" and session.changes is None
        assert not (session.run_dir / "diff.patch").exists()
        assert (session.run_dir / "last-valid-diff.patch").is_file()
        assert "new patch" in session.diff(), "the new work must remain pending"
    finally:
        session.close()


def test_partial_apply_keeps_new_tests_and_binary_assets_pending(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"changed\n")
        session.ws.write("test_added.py", b"def test_new():\n    assert True\n")
        session.ws.write("asset.bin", b"\x00\x01asset")
        session.ws.write("file [one].txt", b"literal path\n")
        # Applying must preserve the user's unrelated staged work too.
        (session.root / "user.txt").write_text("user work\n")
        subprocess.run(["git", "add", "user.txt"], cwd=session.root, check=True)
        assert session.apply(["a.py"])
        assert (session.root / "a.py").read_text() == "changed\n"
        assert not (session.root / "asset.bin").exists()
        assert set(session.changes.files) == {"test_added.py", "asset.bin", "file [one].txt"}
        assert session.apply(["test_added.py", "asset.bin", "file [one].txt"])
        assert (session.root / "asset.bin").read_bytes() == b"\x00\x01asset"
        assert (session.root / "test_added.py").is_file()
        assert (session.root / "file [one].txt").read_text() == "literal path\n"
        assert session.diff() == ""
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=session.root, text=True)
        assert staged.strip() == "user.txt"
    finally:
        session.close()


def test_apply_preserves_git_file_moves_symlinks_and_executable_modes(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        result = session.ws.run("mv a.py 'moved [one].py' && chmod +x 'moved [one].py' && ln -s 'moved [one].py' link")
        assert result.exit_code == 0
        assert session.apply()
        assert not (session.root / "a.py").exists()
        assert (session.root / "moved [one].py").read_text() == "x\n"
        assert (session.root / "moved [one].py").stat().st_mode & 0o100
        assert (session.root / "link").is_symlink()
        assert (session.root / "link").readlink() == Path("moved [one].py")
        assert session.diff() == ""
    finally:
        session.close()


def test_apply_handles_files_replaced_by_directories_and_back(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        assert session.ws.run("mv a.py saved && mkdir a.py && mv saved a.py/item.txt").exit_code == 0
        assert session.apply()
        assert (session.root / "a.py" / "item.txt").read_text() == "x\n"
        assert session.ws.run("mv a.py/item.txt saved && rmdir a.py && mv saved a.py").exit_code == 0
        assert session.apply()
        assert (session.root / "a.py").read_text() == "x\n"
        assert session.diff() == ""
    finally:
        session.close()


def test_baseline_recording_failure_can_be_retried_without_reapplying(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"changed\n")
        with (
            patch("prax_bot.runtime.apply.advance_baseline", side_effect=WorkspaceError("recording failed")),
            pytest.raises(WorkspaceError, match="recording failed"),
        ):
            session.apply()
        assert (session.root / "a.py").read_text() == "changed\n"
        assert json.loads((session.run_dir / "run.json").read_text())["pending_apply"]
        with pytest.raises(WorkspaceError, match="retry /apply"):
            session.instruct("more work")
        with patch("prax_bot.runtime.apply.apply_diff", side_effect=AssertionError("already applied")):
            assert session.apply()
        assert session.diff() == ""
        assert not (session.run_dir / "apply.json").exists()
    finally:
        session.close()


def test_final_record_failure_keeps_the_apply_recoverable(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    record = session.record

    def fail_final_record():
        if session.has_applied and session._pending_apply is None:
            raise OSError("record unavailable")
        record()

    try:
        session.ws.write("a.py", b"changed\n")
        with patch.object(session, "record", side_effect=fail_final_record), pytest.raises(OSError):
            session.apply()
        assert load_run(session.run_id, session.settings).pending_apply is not None
        with patch("prax_bot.runtime.apply.apply_diff", side_effect=AssertionError("already applied")):
            assert session.apply()
        assert session.diff() == "" and (session.root / "a.py").read_text() == "changed\n"
        assert load_run(session.run_id, session.settings).pending_apply is None
    finally:
        session.close()


def test_legacy_pending_apply_records_still_resume(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"changed\n")
        changes = session.ws.changes()
        apply_diff(session.root, changes.patch)
        info = json.loads((session.run_dir / "run.json").read_text())
        metadata = dataclasses.asdict(changes)
        metadata.pop("patch")
        info["pending_apply"] = metadata
        (session.run_dir / "pending-apply.patch").write_text(changes.patch)
        (session.run_dir / "run.json").write_text(json.dumps(info))
        saved = load_run(session.run_id, session.settings)
        workspace = reopen_workspace(saved, len(saved.checkpoints) - 1, session.settings, new_run_id="legacy")
        assert workspace.diff() == "" and workspace.read("a.py") == b"changed\n"
        assert (session.root / "a.py").read_text() == "changed\n"
    finally:
        session.close()


def test_resume_rejects_another_repository_before_opening_it(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        other = git_repo(tmp_path / "other")
        directory = make_run(tmp_path, "other-run")
        info = json.loads((directory / "run.json").read_text())
        info["repo"] = str(other)
        (directory / "run.json").write_text(json.dumps(info))
        workspace = session.ws
        with (
            patch("prax_bot.runtime.session.reopen_workspace", side_effect=AssertionError("must not open")),
            pytest.raises(ValueError, match="different repository"),
        ):
            session.resume("other-run")
        assert session.ws is workspace and session.root != other
    finally:
        session.close()


def test_reopening_an_unfinished_apply_recovers_only_the_applied_files(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        session.ws.write("a.py", b"changed\n")
        session.ws.write("test_new.py", b"new test\n")
        with (
            patch("prax_bot.runtime.apply.advance_baseline", side_effect=WorkspaceError("recording failed")),
            pytest.raises(WorkspaceError, match="recording failed"),
        ):
            session.apply(["a.py"])
    finally:
        session.close()
    saved = load_run(session.run_id, session.settings)
    with pytest.raises(ValueError, match="last checkpoint"):
        reopen_workspace(saved, 0, session.settings)
    workspace = reopen_workspace(saved, len(saved.checkpoints) - 1, session.settings, new_run_id="recovered-apply")
    assert workspace.changes().files == ("test_new.py",)
    assert workspace.read("a.py") == (session.root / "a.py").read_bytes() == b"changed\n"
    assert not (session.root / "test_new.py").exists()


def test_session_resume_records_the_actual_snapshot_location(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        directory = make_run(tmp_path, "previous")
        info = json.loads((directory / "run.json").read_text())
        info["repo"] = str(session.root)
        (directory / "run.json").write_text(json.dumps(info))
        make_snapshot(tmp_path, "previous", 1)
        session.resume("previous")
        saved = load_run(session.run_id, session.settings)
        workspace = reopen_workspace(saved, len(saved.checkpoints) - 1, session.settings, new_run_id="reopened")
        assert workspace.read("a.py") == b"y\n"
        assert session.apply()
        assert (session.root / "a.py").read_text() == "y\n"
    finally:
        session.close()


def test_effort_updates_only_effort_and_its_source(tmp_path: Path) -> None:
    custom = tmp_path / "prompt.md"
    custom.write_text("custom instructions")
    session = make_session(tmp_path, context_budget=24000, investigate_model="probe", system_prompt_file=str(custom))
    try:
        before = dataclasses.asdict(session.settings)
        llm = session.llm
        session.set_effort("high")
        after = dataclasses.asdict(session.settings)
        before.pop("effort")
        after.pop("effort")
        before.pop("sources")
        after.pop("sources")
        assert before == after
        assert session.llm is llm and llm.effort == "high"
        with pytest.raises(ValueError):
            session.set_effort("invalid")
        assert session.settings.effort == "high"
    finally:
        session.close()


def test_undo_restores_context_as_well_as_files(tmp_path: Path) -> None:
    model = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="b\n")),
            reply(None, call("finish", summary="added b")),
            reply(None, call("write_file", path="c.py", content="c\n")),
            reply(None, call("finish", summary="added c")),
            reply(None, call("write_file", path="d.py", content="d\n")),
            reply(None, call("finish", summary="added d")),
        ]
    )
    session = make_session(tmp_path, llm=model)
    try:
        session.instruct("add b")
        session.instruct("add c")
        assert session.undo()
        assert session.last_summary == "added b" and session.instructions == []
        assert "c.py" not in session.changes.files
        session.instruct("add d")
        task = next(m["content"] for m in model.requests[4][0] if m["role"] == "user")
        assert "added b" in task and "added c" not in task
    finally:
        session.close()
