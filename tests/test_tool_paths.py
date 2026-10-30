from __future__ import annotations

from pathlib import Path

import pytest

from prax_bot.core.tools import ToolBox
from prax_bot.workspace.local import LocalWorkspace
from prax_bot.workspace.repository import SCRATCH_DIR


@pytest.fixture(params=["/work", "/testbed"])
def workspace(tmp_path: Path, request: pytest.FixtureRequest) -> LocalWorkspace:
    root = tmp_path / "ws"
    current = root / "current"
    (current / "src").mkdir(parents=True)
    (current / "tests").mkdir()
    (current / "src/a.py").write_text("def before():\n    return 1\n")
    (current / "tests/test_a.py").write_text("assert True\n")
    (current / ".env").write_text("EXAMPLE=1\n")
    ws = LocalWorkspace(root)
    ws.workdir = request.param
    return ws


@pytest.mark.parametrize(
    ("read_path", "edit_path"),
    [
        ("src/a.py", "{root}/src/a.py"),
        ("{root}/src/a.py", "src/a.py"),
        ("src/a.py", "./src/a.py"),
        ("src/../src/a.py", "src/a.py"),
    ],
)
def test_aliases_share_read_and_edit_evidence(workspace: LocalWorkspace, read_path: str, edit_path: str) -> None:
    read_path, edit_path = (p.format(root=workspace.workdir) for p in (read_path, edit_path))
    box = ToolBox(workspace)
    assert not box.call("read_file", {"path": read_path}).error
    assert not box.call("edit_file", {"path": edit_path, "old": "return 1", "new": "return 2"}).error
    # The edit refreshes evidence for every spelling, but not after an external change.
    assert not box.call("edit_file", {"path": read_path, "old": "return 2", "new": "return 3"}).error
    workspace.write("src/a.py", b"def before():\n    return 4\n")
    result = box.call("edit_file", {"path": edit_path, "old": "return 4", "new": "return 5"})
    assert result.error and "changed since you last read" in result.output
    assert workspace.read("src/a.py").endswith(b"return 4\n")


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "{root}/tests/test_a.py",
        "src/../tests/test_a.py",
        SCRATCH_DIR + "/../../..{root}/tests/test_a.py",
        ".env",
        "./.env",
        "{root}/.env",
    ],
)
def test_protection_matches_normalized_repository_paths(workspace: LocalWorkspace, path: str) -> None:
    path = path.format(root=workspace.workdir)
    original = workspace.read(path).decode()
    box = ToolBox(workspace, protected=("tests/*", ".env"))
    assert not box.call("read_file", {"path": path}).error
    for name, args in (
        ("edit_file", {"path": path, "old": original, "new": "changed\n"}),
        ("write_file", {"path": path, "content": "changed\n"}),
    ):
        result = box.call(name, args)
        assert result.error and "is protected" in result.output
    assert workspace.read(path).decode() == original


def test_new_files_and_scratch_remain_writable(workspace: LocalWorkspace) -> None:
    box = ToolBox(workspace, protected=("tests/*", ".env", "*"))
    scratch = f"{SCRATCH_DIR}/.env"
    assert not box.call("write_file", {"path": scratch, "content": "scratch\n"}).error
    assert not box.call("edit_file", {"path": scratch, "old": "scratch", "new": "revised"}).error
    assert workspace.read(scratch) == b"revised\n"
    assert not box.repo_edited
    assert not box.call("write_file", {"path": f"{workspace.workdir}/tests/new.py", "content": "new\n"}).error
    assert box.repo_edited
    result = box.call("edit_file", {"path": "tests/new.py", "old": "new", "new": "revised"})
    assert result.error and "is protected" in result.output


@pytest.mark.parametrize("selection", [{"symbol": "absent"}, {"start_line": 99}, {"start_line": 2, "end_line": 1}])
def test_failed_reads_do_not_create_or_refresh_evidence(workspace: LocalWorkspace, selection: dict) -> None:
    box = ToolBox(workspace)
    assert box.call("read_file", {"path": "src/a.py", **selection}).error
    result = box.call("edit_file", {"path": "src/a.py", "old": "return 1", "new": "return 2"})
    assert result.error and "have not read" in result.output
    assert not box.call("read_file", {"path": "src/a.py"}).error
    workspace.write("src/a.py", b"def before():\n    return 3\n")
    assert box.call("read_file", {"path": "src/a.py", **selection}).error
    result = box.call("edit_file", {"path": "src/a.py", "old": "return 3", "new": "return 4"})
    assert result.error and "changed since you last read" in result.output
    assert workspace.read("src/a.py").endswith(b"return 3\n")


def test_empty_read_records_current_content(workspace: LocalWorkspace) -> None:
    workspace.write("empty.py", b"")
    box = ToolBox(workspace)
    assert not box.call("read_file", {"path": "empty.py"}).error
    result = box.call("edit_file", {"path": "empty.py", "old": "missing", "new": "added"})
    assert result.error and "old text was not found" in result.output


def test_outline_and_compaction_share_cache_across_aliases(workspace: LocalWorkspace) -> None:
    box = ToolBox(workspace)
    aliases = ["src/a.py", "./src/a.py", f"{workspace.workdir}/src/a.py"]
    definitions = box.definitions(aliases)
    assert list(definitions) == aliases
    assert all(value == [(1, "def before():")] for value in definitions.values())
    assert workspace.commands == 1
    read = box.call("read_file", {"path": aliases[-1]})
    assert "def before():" in box.compact("read_file", read.output)
    assert not box.call("edit_file", {"path": aliases[-1], "old": "before", "new": "after"}).error
    assert "def after():" in box.call("outline", {"path": aliases[0]}).output
    assert "def before():" not in box.call("outline", {"path": aliases[1]}).output
    assert workspace.commands == 2
    assert "def after():" in box.compact("read_file", box.call("read_file", {"path": aliases[-1]}).output)


def test_first_absolute_outline_uses_workspace_shell_paths(workspace: LocalWorkspace) -> None:
    result = ToolBox(workspace).call("outline", {"path": f"{workspace.workdir}/src/a.py"})
    assert not result.error and "def before():" in result.output
