"""Forks: checks run on a clean image that holds exactly the diff an apply would write, the same checks run there
without the diff in parallel, and their difference says which failures belong to the change."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest

from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, attributed
from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.ui.report import render_html
from prax_bot.workspace import sandbox
from prax_bot.workspace.local import LocalWorkspace, clone_dir
from prax_bot.workspace.sandbox import CLEAN_PATCH, SandboxWorkspace
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import FakeLLM, ShellFakeWorkspace, base, call, fake_client, reply
from tests.helpers import git_repo, make_session

OLD = "tests/test_old.py::test_old"
NEW = "tests/test_new.py::test_new"
# fails with OLD on any tree, and with NEW as well once b.py exists: what a suite with a standing failure looks like
SUITE = f'echo "FAILED {OLD} - boom"; test -f b.py && echo "FAILED {NEW} - bang"; exit 1'


def local_workspace(tmp_path: Path) -> LocalWorkspace:
    repo = git_repo(tmp_path / "repo")
    root = tmp_path / "local" / "workspace"
    root.mkdir(parents=True)
    clone_dir(repo, root / "current")
    workspace = LocalWorkspace(root, command_timeout=30)
    workspace.mark_clean()
    return workspace


def writes_b_and_a_scratch_file() -> FakeLLM:
    return FakeLLM(
        [
            reply(None, call("write_file", path="repro_check.py", content="print('scratch')\n")),
            reply(None, call("write_file", path="b.py", content="b\n")),
            reply(None, call("finish", summary="done")),
        ]
    )


def test_a_local_fork_holds_the_clean_snapshot_and_the_patch_and_nothing_else(tmp_path: Path) -> None:
    workspace = local_workspace(tmp_path)
    workspace.write("b.py", b"b\n")
    workspace.write("repro_check.py", b"scratch\n")
    changes = workspace.changes()
    assert changes.flagged == {"repro_check.py"}
    applicable = workspace.changes([name for name in changes.files if name not in changes.flagged])
    checkpoints = len(workspace.checkpoints)

    assert workspace.run_clean("ls").stdout.split() == ["a.py"], "without a patch the fork is the baseline"
    assert workspace.run_clean("ls; touch made-in-the-fork", patch=applicable.patch).stdout.split() == ["a.py", "b.py"]
    assert len(workspace.checkpoints) == checkpoints and not (workspace.current / "made-in-the-fork").exists()
    assert sorted(p.name for p in workspace.current.iterdir() if p.name != ".git") == ["a.py", "b.py", "repro_check.py"]
    with pytest.raises(WorkspaceError, match="does not apply to the clean image"):
        workspace.run_clean(
            "ls", patch="diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n"
        )


def test_the_clean_snapshot_follows_an_apply_and_a_retry_is_harmless(tmp_path: Path) -> None:
    workspace = local_workspace(tmp_path)
    assert workspace.clean == "local:snap-0" and workspace.checkpoints[0].clean == "local:snap-0"
    workspace.write("b.py", b"b\n")
    patch = workspace.changes().patch
    workspace.advance_clean(patch)
    assert workspace.clean == "local:clean-0"
    workspace.advance_clean(patch)  # an interrupted apply is finished by running it again
    assert workspace.run_clean("cat b.py").stdout == "b\n"
    workspace.write("c.py", b"c\n")
    assert workspace.checkpoints[-1].clean == workspace.clean, "a checkpoint records the clean image of its moment"

    events: list[tuple[str, dict]] = []
    workspace.on_event = lambda kind, **payload: events.append((kind, payload))
    workspace.advance_clean("not a patch")
    assert workspace.clean is None and events[0][0] == "sandbox.clean_lost", "it is forgotten, and the apply goes on"
    with pytest.raises(WorkspaceError, match="no clean snapshot"):
        workspace.run_clean("ls")


def test_a_sandbox_fork_runs_disposably_from_the_clean_image_with_the_patch_uploaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[dict] = []
    workspace = SandboxWorkspace(fake_client(log), base(log), command_timeout=30)
    workspace.mark_clean()
    workspace.run("touch marker")  # the agent's work moves the workspace, not the clean image
    assert workspace.clean == "base" and workspace.checkpoints[-1].clean == "base"

    result = workspace.run_clean("pytest -q", patch="diff --git a/x b/x\n", timeout=45)
    forked = log[-1]
    assert result.exit_code == 0 and forked["disposable"] is True and forked["timeout"] == 45
    assert forked["files"] == {CLEAN_PATCH: b"diff --git a/x b/x\n"}
    assert forked["shell"].startswith("git apply --binary") and forked["shell"].endswith("\npytest -q")
    assert len(workspace.checkpoints) == 2 and workspace.commands == 2 and workspace.total_cost > 0
    assert log[-1]["cwd"] == "/work" and workspace.run_clean("pytest -q").command == "pytest -q"
    assert log[-1]["files"] == {} and log[-1]["shell"] == "pytest -q", "no patch, no apply: the baseline itself"

    before = workspace.image_id
    workspace.advance_clean("diff --git a/x b/x\n")
    assert log[-1]["disposable"] is False and workspace.clean not in ("base", None, before)
    assert workspace.image_id == before and len(workspace.checkpoints) == 2, "the working chain does not move"

    events: list[str] = []
    workspace.on_event = lambda kind, **payload: events.append(kind)
    monkeypatch.setattr(sandbox, "apply_patch_shell", lambda path, skip_if_applied=False: "fail")
    workspace.advance_clean("diff --git a/x b/x\n")
    assert workspace.clean is None and events == ["sandbox.clean_lost"]


def test_checks_run_in_a_fork_that_holds_what_an_apply_would_write(tmp_path: Path) -> None:
    # passes only where b.py exists and the scratch file does not: the fork, never the workspace
    session = make_session(
        tmp_path, llm=writes_b_and_a_scratch_file(), test_command="test -f b.py && test ! -f repro_check.py"
    )
    session.ws.mark_clean()
    try:
        result, _ = session.instruct("add b")
        assert result.finished and session.verification_status == "passed"
        check = session.checks[0]
        assert check.where == "fork" and check.image == session.ws.image_id
        assert session.ws.run("test -f b.py && test ! -f repro_check.py", mutate=False).exit_code == 1
        system = session.llm.requests[0][0][0]["content"]
        assert "fresh copy of the repository that holds only the diff an apply would write" in system
        record = json.loads((session.run_dir / "run.json").read_text())
        assert record["verification"]["checks"][0]["where"] == "fork"
        assert record["checkpoints"][0]["clean"] == "local:snap-0"
        assert "ran in a fork of the clean image" in render_html(load_run(session.run_id, session.settings))
    finally:
        session.close()


def test_without_a_clean_image_checks_run_in_the_workspace_as_before(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=writes_b_and_a_scratch_file(), test_command="test -f repro_check.py")
    try:
        session.instruct("add b")
        assert session.ws.clean is None and session.checks[0].where == "workspace" and session.checks[0].passed
        assert session.checks[0].attribution == "" and not session._baselines
        assert "fresh copy of the repository" not in session.llm.requests[0][0][0]["content"]
    finally:
        session.close()


def test_a_parallel_baseline_run_says_which_failures_belong_to_the_change(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=writes_b_and_a_scratch_file(), test_command=SUITE)
    session.ws.mark_clean()
    events: list[tuple[str, dict]] = []
    observer = session._observer
    session._observer = lambda kind, **payload: (events.append((kind, payload)), observer(kind, **payload))[1]
    try:
        session.instruct("add b")
        check = session.checks[0]
        assert session.verification_status == "failed" and check.baseline_exit_code == 1
        assert check.new_failures == (NEW,) and check.known_failures == (OLD,)
        assert check.attribution == f"1 new with this change: {NEW}; 1 also fails without it"
        baselines = [payload for kind, payload in events if kind == "verification.baseline"]
        assert len(baselines) == 1 and baselines[0]["failures"] == (OLD,) and baselines[0]["where"] == "fork"
        result = next(payload for kind, payload in events if kind == "verification.result")
        assert result["attribution"] == check.attribution and result["passed"] is False
        assert session._known_failures() == {OLD}, "what the toolbox is told while the model works"
        assert session.state()["checks"][0]["attribution"] == check.attribution
    finally:
        session.close()


def test_a_slow_baseline_overlaps_the_check_instead_of_delaying_it(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=writes_b_and_a_scratch_file(), test_command=SUITE)
    session.ws.mark_clean()
    check_started = Event()
    overlapped: list[bool] = []
    run_clean = session.ws.run_clean

    def slow_baseline(command: str, *, patch: str = "", timeout: int | None = None):
        if patch:
            check_started.set()
        else:
            overlapped.append(check_started.wait(10))  # still running when the check with the diff starts
        return run_clean(command, patch=patch, timeout=timeout)

    session.ws.run_clean = slow_baseline
    try:
        session.instruct("add b")
        assert overlapped == [True], "verification did not wait for the baseline before starting the check"
        assert session.checks[0].known_failures == (OLD,) and session.checks[0].new_failures == (NEW,)
    finally:
        session.close()


def test_with_nothing_to_apply_the_baseline_run_is_the_check(tmp_path: Path) -> None:
    llm = FakeLLM([reply(None, call("finish", summary="nothing")), reply(None, call("finish", summary="really"))])
    session = make_session(tmp_path, llm=llm, test_command=SUITE)
    session.ws.mark_clean()
    runs: list[str] = []
    run_clean = session.ws.run_clean
    session.ws.run_clean = lambda command, **kw: (runs.append(kw.get("patch", "")), run_clean(command, **kw))[1]
    try:
        session.instruct("look around")
        assert runs == [""], "one run in a fork, not two"
        check = session.checks[0]
        assert check.where == "fork" and check.known_failures == (OLD,) and check.new_failures == ()
        assert check.attribution == "1 also fails without it" and check.image == session.ws.image_id
    finally:
        session.close()


def test_an_apply_moves_the_clean_image_and_retires_the_old_baseline_runs(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=writes_b_and_a_scratch_file(), test_command="test -f b.py")
    session.ws.mark_clean()
    try:
        session.instruct("add b")
        assert session.ws.run_clean("test -f b.py").exit_code == 1 and session._baselines
        assert session.apply() is True
        assert session.ws.clean == "local:clean-0" and not session._baselines
        assert session.ws.run_clean("test -f b.py && test ! -f repro_check.py").exit_code == 0
        assert (session.root / "b.py").exists() and not (session.root / "repro_check.py").exists()
        assert session.ws.checkpoints[-1].clean == "local:clean-0"
    finally:
        session.close()


def test_attribution_needs_a_baseline_that_ran() -> None:
    failed = CheckResult("tests", "pytest", "img", 1, "", failures=(OLD, NEW))
    assert attributed(failed, None) is failed
    unavailable = CheckResult("tests", "pytest", "img", None, "", error="boom")
    incomplete = attributed(failed, unavailable)
    assert incomplete.baseline is unavailable and incomplete.attribution == ""
    assert incomplete.baseline_exit_code is None and incomplete.new_failures == incomplete.known_failures == ()
    clean = attributed(failed, CheckResult("tests", "pytest", "img", 0, ""))
    assert clean.new_failures == (OLD, NEW) and "2 new with this change" in clean.attribution
    opaque = attributed(CheckResult("lint", "ruff", "img", 1, ""), CheckResult("lint", "ruff", "img", 1, ""))
    assert opaque.attribution == "also fails without this change"
    assert attributed(
        CheckResult("lint", "ruff", "img", 1, ""), CheckResult("lint", "ruff", "img", 0, "")
    ).attribution == ("passes without this change")
    assert (
        attributed(
            CheckResult("tests", "pytest", "img", 0, ""), CheckResult("tests", "pytest", "img", 1, "")
        ).attribution
        == ""
    )


def test_the_toolbox_learns_baseline_failures_from_the_fork() -> None:
    output = f"FAILED {OLD} - boom\nFAILED {NEW} - bang\n1 passed, 2 failed"

    def note_after_an_edit(known_failures) -> str:
        workspace = ShellFakeWorkspace({"src/a.py": "x\n"}, results=[(1, output, "", False)])
        box = ToolBox(workspace, known_failures=known_failures)
        box.call("read_file", {"path": "src/a.py"})
        box.call("edit_file", {"path": "src/a.py", "old": "x", "new": "y"})
        return box.call("run", {"command": "pytest -q"}).output  # the model never ran the tests before editing

    assert "already failing" not in note_after_an_edit(None), "nothing is known about the baseline"
    assert "already failing" not in note_after_an_edit(set), "the fork is not back yet"
    told = note_after_an_edit(lambda: {OLD})
    assert (
        f"1 of these failures were already failing before your edits: {OLD}" in told
        and NEW not in told.split("note:")[1]
    )


def test_a_resumed_sandbox_run_forks_the_clean_image_of_its_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prax_bot.runtime.runs import Run
    from tests.helpers import settings

    log: list[dict] = []
    base(log)
    monkeypatch.setattr(sandbox, "make_client", lambda: fake_client(log))
    checkpoints = [
        {"step": 0, "image": "base", "label": "baseline", "clean": "base"},
        {"step": 1, "image": "base", "label": "run: older record"},
    ]
    run = Run("20260901T000000Z-aaa", tmp_path, {"checkpoints": checkpoints, "backend": "contree"})
    assert reopen_workspace(run, 0, settings(tmp_path)).clean == "base"
    assert reopen_workspace(run, 1, settings(tmp_path)).clean is None, "records written before forks keep working"
