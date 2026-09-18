from __future__ import annotations

import json

from prax_bot.core.agent import Agent
from prax_bot.core.tools import ToolBox
from prax_bot.workspace.repository import SCRATCH_DIR, resolve_in_workdir
from tests.fakes import FakeLLM, FakeToolBox, FakeWorkspace, ShellFakeWorkspace, call, reply


def test_scratch_dir_is_an_allowed_root() -> None:
    assert resolve_in_workdir(f"{SCRATCH_DIR}/repro.py") == f"{SCRATCH_DIR}/repro.py"
    try:
        resolve_in_workdir("/tmp/other.py")
    except ValueError as e:
        assert "throwaway files belong in" in str(e)
    else:
        raise AssertionError("path outside both roots was accepted")


def test_protected_paths_refuse_edits_and_writes() -> None:
    box = ToolBox(
        FakeWorkspace({"tests/test_a.py": "x\n", "src/a.py": "x\n"}), protected=("tests/*", "*/tests/*", "test_*.py")
    )
    box.call("read_file", {"path": "tests/test_a.py"})
    assert box.call("edit_file", {"path": "tests/test_a.py", "old": "x", "new": "y"}).output.startswith(
        "error: tests/test_a.py is protected and must not be modified."
    )
    assert box.call("write_file", {"path": "tests/test_new.py", "content": "y"}).output.startswith(
        "wrote tests/test_new.py"
    )  # new files are allowed
    assert box.call("write_file", {"path": "/tmp/praxbot/scratch/test_repro.py", "content": "y"}).output.startswith(
        "wrote /tmp/praxbot/scratch/test_repro.py"
    )
    box.call("read_file", {"path": "src/a.py"})
    assert box.call("edit_file", {"path": "src/a.py", "old": "x", "new": "y"}).output.startswith("edited src/a.py")


def test_a_copied_context_placeholder_never_reaches_a_file() -> None:
    from prax_bot.core.context import ELIDED_MARK

    workspace = FakeWorkspace({"src/a.py": "x\n"})
    box = ToolBox(workspace)
    placeholder = f"{ELIDED_MARK} to save context: 1,542 characters]"
    box.call("read_file", {"path": "src/a.py"})
    for name, args in (
        ("edit_file", {"path": "src/a.py", "old": "x", "new": placeholder}),
        ("write_file", {"path": "src/b.py", "content": placeholder}),
    ):
        result = box.call(name, json.dumps(args))
        assert result.error and "placeholder the runtime puts into old calls" in result.output
    assert workspace.files == {"src/a.py": b"x\n"}


def test_empty_finish_is_refused_once() -> None:
    llm = FakeLLM(
        [reply(None, call("finish", summary="nothing to do")), reply(None, call("finish", summary="really nothing"))]
    )
    result = Agent(llm, FakeToolBox(), max_steps=10).run("t")
    assert result.finished and result.summary == "really nothing" and len(llm.requests) == 2
    refused = [m for m in llm.requests[-1][0] if m["role"] == "tool"]
    assert len(refused) == 1 and refused[0]["content"].startswith("error: no changes have been made")
    assert "read-only, already satisfied, or blocked, call finish again" in refused[0]["content"]


def test_search_error_lists_top_level_directories() -> None:
    ws = ShellFakeWorkspace(
        results=[
            (0, "README.md\nsphinx/util.py\ntests/test_a.py\n", "", False),
            (0, "", "", False),
            (2, "", "grep: sphinx/tests: No such file or directory", False),
            (2, "", "grep: sphinx/tests: No such file or directory", False),  # literal fallback
        ]
    )
    box = ToolBox(ws)
    box.repo_map("fix kbd role")
    out = box.call("search", {"pattern": "kbd", "path": "sphinx/tests"}).output
    assert out.startswith("error: grep: sphinx/tests: No such file or directory")
    assert "top-level directories are: sphinx, tests" in out
