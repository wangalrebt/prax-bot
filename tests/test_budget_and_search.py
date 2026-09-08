from __future__ import annotations

from prax_bot.agent import FORCE_FINISH, Agent
from prax_bot.tools import ToolBox
from tests.test_agent import FakeLLM, FakeToolBox, call, reply
from tests.test_review_fixes import ShellFakeWorkspace


def test_turns_without_tool_calls_do_not_consume_the_budget() -> None:
    llm = FakeLLM(
        [
            reply("thinking"),
            reply(None, call("run", command="pytest")),
            reply("still thinking"),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("finish", summary="done")),
        ]
    )
    events: list[tuple[str, dict]] = []
    result = Agent(llm, FakeToolBox(), max_steps=3, on_event=lambda k, **p: events.append((k, p))).run("t")
    assert result.finished and result.steps == 3 and len(llm.requests) == 5
    assert [k for k, _ in events].count("nudge") == 2
    assert llm.requests[-1][1] == FORCE_FINISH  # third tool step is the last one allowed, so finish was forced


def test_budget_still_ends_runs_that_keep_calling_tools() -> None:
    llm = FakeLLM([reply(None, call("run", command="ls")) for _ in range(6)])
    result = Agent(llm, FakeToolBox(), max_steps=2).run("t")
    assert not result.finished and result.reason == "budget" and result.steps == 2 and len(llm.requests) == 2


def test_invalid_regex_falls_back_to_a_literal_search() -> None:
    ws = ShellFakeWorkspace(
        results=[
            (2, "", "grep: Unmatched ( or \\(", False),
            (0, "tabulate/__init__.py:1207:def _align_column(\n", "", False),
        ]
    )
    out = ToolBox(ws).call("search", {"pattern": "_align_column("})
    assert out.startswith("tabulate/__init__.py:1207:def _align_column(")
    assert out.endswith("(the pattern was not a valid regular expression, so it was searched for literally)")
    assert " -E " in ws.commands[0] and " -F " in ws.commands[1]
