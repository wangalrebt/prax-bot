from __future__ import annotations

from pathlib import Path

import pytest

from prax_bot.core.tools import ToolBox
from prax_bot.workspace.local import LocalWorkspace


@pytest.fixture(params=["/work", "/testbed"])
def workspace(tmp_path: Path, request: pytest.FixtureRequest) -> LocalWorkspace:
    root = tmp_path / "workspace"
    (root / "current" / "src").mkdir(parents=True)
    (root / "current" / "src" / "a.py").write_text("def before():\n    return 1\n")
    workspace = LocalWorkspace(root)
    workspace.workdir = request.param
    return workspace


def test_restore_refreshes_outline_and_compaction_for_all_path_aliases(workspace: LocalWorkspace) -> None:
    box = ToolBox(workspace)
    aliases = ["src/a.py", "./src/a.py", f"{workspace.workdir}/src/a.py"]
    assert not box.call("read_file", {"path": aliases[0]}).error
    assert not box.call("edit_file", {"path": aliases[-1], "old": "return 1", "new": "return 2"}).error
    keep_step = workspace.current_step
    assert not box.call("edit_file", {"path": aliases[1], "old": "before", "new": "after"}).error
    assert "def after():" in box.call("outline", {"path": aliases[-1]}).output
    commands = workspace.commands

    restored = box.call("restore", {"step": keep_step})
    assert not restored.error
    assert workspace.read(aliases[0]) == b"def before():\n    return 2\n"
    for path in aliases:
        outline = box.call("outline", {"path": path}).output
        assert "def before():" in outline and "def after():" not in outline
        compacted = box.compact("read_file", box.call("read_file", {"path": path}).output)
        assert "def before():" in compacted and "def after():" not in compacted
    assert workspace.commands == commands + 1, "restored definitions are fetched once for every alias"


def test_failed_restore_keeps_cached_definitions(workspace: LocalWorkspace) -> None:
    box = ToolBox(workspace)
    path = f"{workspace.workdir}/src/a.py"
    assert "def before():" in box.call("outline", {"path": path}).output
    commands = workspace.commands

    assert box.call("restore", {"step": 99}).error
    assert box.defs_for("./src/a.py") == [(1, "def before():")]
    assert "def before():" in box.call("outline", {"path": "src/a.py"}).output
    assert workspace.commands == commands
