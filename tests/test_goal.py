"""Goal runs: rounds on the same workspace until a check passes or the model reports the goal met."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from prax_bot.core.agent import Agent, AgentResult
from prax_bot.core.goal import GoalDriver, goal_task
from prax_bot.core.tools import tool_specs
from prax_bot.runtime.runs import load_run, reopen_workspace
from tests.test_agent import FakeLLM, FakeToolBox, call, reply
from tests.test_resume import make_snapshot
from tests.test_runs import make_run, settings


class CheckWorkspace:
    """Only the piece of a workspace the driver uses: run a check command with scripted exit codes."""

    def __init__(self, exit_codes: list[int]) -> None:
        self.exit_codes = list(exit_codes)
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: int, mutate: bool) -> SimpleNamespace:
        assert mutate is False, "a check must not create a checkpoint"
        self.commands.append(command)
        code = self.exit_codes.pop(0)
        return SimpleNamespace(exit_code=code, stdout=f"exit {code}\n", stderr="", timed_out=False)


def done(summary: str = "did some", goal_met: bool = False, reason: str = "finish") -> AgentResult:
    return AgentResult(True, summary, 3, reason, goal_met)


def test_goal_finish_schema_only_in_goal_mode() -> None:
    plain = next(s for s in tool_specs() if s["function"]["name"] == "finish")
    goal = next(s for s in tool_specs(goal=True) if s["function"]["name"] == "finish")
    assert "goal_met" not in plain["function"]["parameters"]["properties"]
    assert "goal_met" in goal["function"]["parameters"]["properties"]


def test_agent_reports_goal_met_from_finish() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a", old="x", new="y")),
            reply(None, call("finish", summary="all done", goal_met=True)),
        ]
    )
    result = Agent(llm, FakeToolBox(), max_steps=5, goal=True).run("go")
    assert result.finished and result.goal_met is True
    llm = FakeLLM(
        [reply(None, call("edit_file", path="a", old="x", new="y")), reply(None, call("finish", summary="partly"))]
    )
    assert Agent(llm, FakeToolBox(), max_steps=5).run("go").goal_met is False


def test_driver_continues_until_the_check_passes() -> None:
    ws = CheckWorkspace([1, 0])
    d = GoalDriver("make the tests pass", ws, max_rounds=5, check="pytest -q")
    nxt = d.next_instruction(1, done(), "diff a")
    assert nxt and nxt.startswith("Round 2 of 5") and "pytest -q" in nxt and "exit 1" in nxt
    assert d.next_instruction(2, done(), "diff b") is None and d.met is True
    assert ws.commands == ["pytest -q", "pytest -q"] and d.status == "goal met after 2 rounds"


def test_driver_without_a_check_trusts_goal_met() -> None:
    d = GoalDriver("refactor", CheckWorkspace([]), max_rounds=3)
    assert d.next_instruction(1, done(goal_met=False), "d1").startswith("Round 2 of 3")
    assert d.next_instruction(2, done(goal_met=True), "d2") is None and d.met


def test_driver_stops_at_the_round_cap_and_on_no_progress() -> None:
    d = GoalDriver("x", CheckWorkspace([1, 1]), max_rounds=2, check="make test")
    assert d.next_instruction(1, done(), "d1") is not None
    assert d.next_instruction(2, done(), "d2") is None and not d.met and "round cap" in d.status
    d = GoalDriver("x", CheckWorkspace([]), max_rounds=9)
    assert d.next_instruction(1, done(), "same") is not None
    assert d.next_instruction(2, done(), "same") is None and "no change" in d.status
    d = GoalDriver("x", CheckWorkspace([]), max_rounds=9)
    assert d.next_instruction(1, AgentResult(False, "boom", 0, "error"), "d") is None and "error" in d.status


def test_goal_task_states_the_check_and_the_finish_contract() -> None:
    t = goal_task("all tests green", 4, "pytest")
    assert t.startswith("Goal: all tests green") and "up to 4 rounds" in t and "exits 0" in t and "goal_met=true" in t
    assert "You decide" in goal_task("tidy", 2, None)


def test_solve_runs_goal_rounds_automatically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm
    from prax_bot.runtime.session import new_run
    from prax_bot.ui import cli

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, None, "20260902T000000Z-goal")
    llm = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="1\n")),
            reply(None, call("finish", summary="wrote b", goal_met=False)),
            reply(None, call("write_file", path="c.py", content="2\n")),
            reply(None, call("finish", summary="wrote c", goal_met=True)),
        ]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)
    driver = GoalDriver("b and c exist", ws, max_rounds=4)
    run_id, run_dir, events_file, on_event = new_run(s, verbose=False)
    cli._solve(
        s,
        ws,
        tmp_path,
        goal_task("b and c exist", 4, None),
        backend="local",
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=False,
        export=None,
        protect=[],
        started=time.monotonic(),
        record_task="b and c exist",
        goal=True,
        auto_next=driver.next_instruction,
        ask_followup=lambda: "",
        extra=lambda: {"goal": {"rounds": driver.rounds, "met": driver.met}},
    )
    info = json.loads((run_dir / "run.json").read_text())
    assert info["task"] == "b and c exist" and info["goal"] == {"rounds": 2, "met": True}
    assert len(info["followups"]) == 1 and info["followups"][0].startswith("Round 2 of 4")
    patch = (run_dir / "diff.patch").read_text()
    assert "b/b.py" in patch and "b/c.py" in patch
