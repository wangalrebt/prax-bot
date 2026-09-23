"""Running a command against the repository as it was, without discarding work, and what the harness learns from it.

In the benchmark logs the model twice set its own changes aside with git to compare against the original code; once
it never brought them back and the run ended with no diff. The run tool's `original` option does that comparison in a
throwaway copy instead.
"""

from __future__ import annotations

from pathlib import Path

from prax_bot.core.tools import ORIGINAL_UNAVAILABLE, ToolBox
from prax_bot.workspace.local import LocalWorkspace, clone_dir
from prax_bot.workspace.sandbox import SandboxWorkspace
from tests.fakes import ShellFakeWorkspace, base, fake_client
from tests.helpers import git_repo


def local_box(tmp_path: Path) -> ToolBox:
    repo = git_repo(tmp_path / "repo")  # a.py holds "x", committed
    root = tmp_path / "local" / "workspace"
    root.mkdir(parents=True)
    clone_dir(repo, root / "current")
    return ToolBox(LocalWorkspace(root, command_timeout=30))


def edited_box(tmp_path: Path) -> ToolBox:
    box = local_box(tmp_path)
    box.call("read_file", {"path": "a.py"})
    assert box.call("edit_file", {"path": "a.py", "old": "x", "new": "y"}).output.startswith("edited a.py")
    return box


def test_an_original_run_sees_the_repository_as_it_was_and_keeps_nothing(tmp_path: Path) -> None:
    box = edited_box(tmp_path)
    workspace = box.ws
    assert isinstance(workspace, LocalWorkspace)
    box.call("write_file", {"path": "new.py", "content": "print('new')\n"})
    box.call("write_file", {"path": "/tmp/praxbot/scratch/note.txt", "content": "scratch is here\n"})
    checkpoints = len(workspace.checkpoints)

    command = 'ls; cat a.py "$TMPDIR/praxbot/scratch/note.txt"; touch left-behind'
    result = box.call("run", {"command": command, "original": True})
    assert not result.error, result.output
    header, _, rest = result.output.partition("\n")
    assert header.endswith("on the original repository, nothing kept")
    assert rest == "output:\na.py\nx\nscratch is here", "edit and new file set aside, scratch there, nothing appended"
    assert "full output saved" not in result.output
    assert len(workspace.checkpoints) == checkpoints, "no checkpoint: the workspace did not move"
    assert (workspace.current / "a.py").read_text() == "y\n" and (workspace.current / "new.py").exists()
    assert not (workspace.current / "left-behind").exists()
    assert "stash@{" not in box.call("run", {"command": "git stash list"}).output


def test_when_the_changes_cannot_be_set_aside_nothing_runs(tmp_path: Path) -> None:
    box = edited_box(tmp_path)
    assert isinstance(box.ws, LocalWorkspace)
    (box.ws.current / ".git" / "index.lock").write_text("")  # git refuses to touch the index while this exists
    result = box.call("run", {"command": "echo RAN-ON-THE-EDITED-TREE", "original": True})
    assert result.error and ORIGINAL_UNAVAILABLE in result.output
    assert "RAN-ON-THE-EDITED-TREE" not in result.output


def test_an_original_run_on_the_sandbox_is_a_disposable_run_that_stashes_first() -> None:
    log: list[dict] = []
    workspace = SandboxWorkspace(fake_client(log), base(log), command_timeout=30)
    box = ToolBox(workspace)
    box.call("run", {"command": "python -m pytest -q", "original": True})
    shell = log[-1]["shell"]
    assert log[-1]["disposable"] is True and len(workspace.checkpoints) == 1
    assert shell.index("stash push --include-untracked") < shell.index("python -m pytest -q")
    assert "-c user.email=praxbot@localhost" in shell, "a sandbox image has no git identity, and stash needs one"


def test_failures_on_the_original_repository_count_as_already_failing() -> None:
    old = "FAILED tests/test_t.py::test_old - boom\n"
    new = "FAILED tests/test_t.py::test_new - bang\n"
    workspace = ShellFakeWorkspace(
        {"src/a.py": "x\n"}, results=[(1, old + "1 failed", "", False), (1, old + new + "2 failed", "", False)]
    )
    box = ToolBox(workspace)
    box.call("read_file", {"path": "src/a.py"})
    box.call("edit_file", {"path": "src/a.py", "old": "x", "new": "y"})  # the model edits before running any test
    first = box.call("run", {"command": "python -m pytest -q", "original": True}).output
    assert "note: 1 tests fail on the original repository" in first and "tests/test_t.py::test_old" in first
    assert "stash push" in workspace.commands[0]
    after = box.call("run", {"command": "python -m pytest -q"}).output
    assert "1 of these failures were already failing before your edits: tests/test_t.py::test_old" in after
    assert "test_new" not in after.split("note:")[1].split("already failing")[1]
