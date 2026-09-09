from __future__ import annotations

from prax_bot.core.agent import Agent
from prax_bot.core.llm import Usage
from prax_bot.core.tools import ToolBox, compact_tool_output, rank_files, references_in_output
from tests.test_agent import FakeToolBox, call, reply
from tests.test_review_fixes import ShellFakeWorkspace


class RoutingFakeLLM:
    model = "super"

    def __init__(self, replies):
        self.replies = list(replies)
        self.models: list[str | None] = []
        self.usage = Usage()

    def complete(self, messages, tools, *, tool_choice="auto", temperature=0.2, model=None):
        self.models.append(model)
        self.usage.add(model or self.model, 10, 1)
        return self.replies.pop(0)


def test_investigation_model_is_used_until_the_first_edit_or_step_limit() -> None:
    llm = RoutingFakeLLM(
        [
            reply(None, call("read_file", path="a.py")),
            reply(None, call("search", pattern="x")),
            reply(None, call("edit_file", path="a.py", old="a", new="b")),
            reply(None, call("run", command="pytest")),
            reply(None, call("finish", summary="done")),
        ]
    )
    Agent(llm, FakeToolBox(), max_steps=10, investigate_model="ultra", investigate_steps=6).run("t")
    assert llm.models == ["ultra", "ultra", "ultra", None, None]
    assert llm.usage.by_model == {
        "ultra": {"calls": 3, "prompt_tokens": 30, "completion_tokens": 3},
        "super": {"calls": 2, "prompt_tokens": 20, "completion_tokens": 2},
    }

    llm2 = RoutingFakeLLM(
        [reply(None, call("read_file", path="a.py")) for _ in range(3)] + [reply(None, call("finish", summary="s"))] * 2
    )
    Agent(llm2, FakeToolBox(), max_steps=10, investigate_model="ultra", investigate_steps=2).run("t")
    assert llm2.models == ["ultra", "ultra", None, None, None]


def test_usage_totals_stay_consistent_with_per_model_breakdown() -> None:
    u = Usage()
    u.add("a", 5, 1)
    u.add("b", 7, 2)
    assert (u.prompt_tokens, u.completion_tokens, u.calls, u.total_tokens) == (12, 3, 2, 15)


def test_project_instructions_are_read_from_agents_md() -> None:
    box = ToolBox(ShellFakeWorkspace({"AGENTS.md": "Run tests with `make test`.\n"}))
    assert box.project_instructions() == "Project instructions from AGENTS.md:\nRun tests with `make test`."
    assert ToolBox(ShellFakeWorkspace({})).project_instructions() == ""


def test_rank_files_prefers_mentioned_files_and_demotes_tests() -> None:
    paths = ["tests/test_pad.py", "tabulate/__init__.py", "docs/x.md", "setup.py", "tabulate/cli.py"]
    task = "Left-aligned columns in tabulate are too narrow; run python -m pytest tests/test_pad.py"
    assert rank_files(paths, task)[:2] == ["tests/test_pad.py", "tabulate/__init__.py"]
    assert "docs/x.md" not in rank_files(paths, task)


def test_references_in_output_finds_repo_lines_and_skips_site_packages() -> None:
    out = 'File "/work/pkg/mod.py", line 12, in f\n  File "/usr/lib/python3.12/site-packages/x.py", line 3\ntests/test_a.py:7: AssertionError\npkg/mod.py:12: x'
    assert references_in_output(out) == [("pkg/mod.py", 12), ("tests/test_a.py", 7)]


def test_compact_tool_output_keeps_the_gist() -> None:
    defs = lambda p: [(10, "def a():"), (300, "def b():"), (900, "class C:")]  # noqa: E731
    read = "pkg/mod.py lines 1-400 of 1200\n  1| import os\n  2| ...\n"
    assert (
        compact_tool_output("read_file", read, defs)
        == "[compacted] pkg/mod.py lines 1-400 of 1200; definitions in that range: 10:def a():, 300:def b():. Read it again if you need the text."
    )
    run = "exit code 1 · 0.3s · checkpoint 4\nstdout:\n....F\nFAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 4 passed in 0.1s"
    assert (
        compact_tool_output("run", run)
        == "[compacted] exit code 1 · 0.3s · checkpoint 4 | ....F | FAILED tests/test_a.py::test_x - assert 1 == 2 | 1 failed, 4 passed in 0.1s"
    )
    search = "a.py:3:x = 1\nb.py:9:x = 2\n"
    assert compact_tool_output("search", search) == "[compacted] search: 2 matches at a.py:3, b.py:9"
    assert compact_tool_output("edit_file", "edited a.py") == "edited a.py"


def test_repo_map_is_budgeted_and_ranked() -> None:
    ws = ShellFakeWorkspace(
        results=[
            (0, "README.md\ntabulate/__init__.py\ntests/test_output.py\n", "", False),
            (
                0,
                "tabulate/__init__.py:10:def _padright(width, s):\ntabulate/__init__.py:40:class Foo:\ntests/test_output.py:5:def test_simple():\n",
                "",
                False,
            ),
        ]
    )
    box = ToolBox(ws)
    out = box.repo_map("columns in tabulate are too narrow", budget=10_000)
    assert out.startswith(
        "tabulate/__init__.py (2 definitions)\n  10: def _padright(width, s):\n  40: class Foo:\ntests/test_output.py (1 definitions)"
    )
    assert "grep -nHE" in ws.commands[1] and "README.md" not in ws.commands[1]
