"""The opt-in finish review offers one bounded chance to inspect and repair a change."""

from __future__ import annotations

import pytest

from prax_bot.core.agent import FORCE_FINISH, Agent
from prax_bot.core.tools import ToolBox
from tests.fakes import FakeLLM, FakeToolBox, FakeWorkspace, call, reply


def test_review_can_check_and_repair_before_accepting_the_final_summary() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="initial fix", goal_met=True)),
            reply(None, call("run", command="python check_boundary.py")),
            reply(None, call("edit_file", path="a.py", old="y", new="z")),
            reply(None, call("run", command="python check_boundary.py")),
            reply(None, call("finish", summary="fixed boundary; broader checks pending", goal_met=False)),
        ]
    )
    box = FakeToolBox()
    events: list[tuple[str, dict]] = []
    result = Agent(
        llm, box, max_steps=20, goal=True, review_before_finish=True, on_event=lambda k, **p: events.append((k, p))
    ).run("fix it")
    assert result.finished and result.steps == 6 and not result.goal_met
    assert result.summary == "fixed boundary; broader checks pending"
    assert [name for name, _ in box.calls] == ["edit_file", "run", "edit_file", "run"]
    reviews = [p for k, p in events if k == "review.requested"]
    assert reviews == [{"step": 2, "deadline": 7}]
    assert len([p for k, p in events if k == "finish"]) == 1
    feedback = llm.requests[2][0][-1]
    assert feedback["role"] == "tool" and feedback["tool_call_id"] == "call-finish"
    assert "at most 5 more tool turns" in feedback["content"]
    assert "exception path" in feedback["content"]


def test_review_is_disabled_by_default() -> None:
    llm = FakeLLM(
        [reply(None, call("edit_file", path="a.py", old="x", new="y")), reply(None, call("finish", summary="done"))]
    )
    result = Agent(llm, FakeToolBox(), max_steps=20).run("fix it")
    assert result.finished and result.steps == 2 and len(llm.requests) == 2


@pytest.mark.parametrize("scratch", [False, True])
def test_review_skips_tasks_without_tracked_repository_edits(scratch: bool) -> None:
    replies = (
        [reply(None, call("write_file", path="/tmp/praxbot/scratch/check.py", content="print(1)\n"))] if scratch else []
    )
    llm = FakeLLM(
        [*replies, reply(None, call("finish", summary="no changes")), reply(None, call("finish", summary="read only"))]
    )
    events: list[str] = []
    result = Agent(
        llm, ToolBox(FakeWorkspace()), review_before_finish=True, on_event=lambda k, **p: events.append(k)
    ).run("inspect")
    assert result.finished and "review.requested" not in events


@pytest.mark.parametrize("max_steps, reviewed", [(3, False), (4, True)])
def test_review_needs_a_work_turn_and_a_finish_turn_left(max_steps: int, reviewed: bool) -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
            reply(None, call("run", command="check")),
            reply(None, call("finish", summary="reviewed")),
        ]
    )
    events: list[str] = []
    result = Agent(
        llm, FakeToolBox(), max_steps=max_steps, review_before_finish=True, on_event=lambda k, **p: events.append(k)
    ).run("fix it")
    assert result.finished and ("review.requested" in events) == reviewed
    assert result.steps == (4 if reviewed else 2)
    if reviewed:
        assert llm.requests[2][1] == "auto" and llm.requests[3][1] == FORCE_FINISH


def test_review_deadline_stops_a_model_that_keeps_working() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
            *(reply(None, call("read_file", path=f"{i}.py")) for i in range(6)),
        ]
    )
    result = Agent(llm, FakeToolBox(), max_steps=20, review_before_finish=True).run("fix it")
    assert not result.finished and result.reason == "budget" and result.steps == 7
    assert len(llm.requests) == 7 and llm.requests[-1][1] == FORCE_FINISH
    assert "(step 6 of 7)" in llm.requests[-1][0][-1]["content"]
    assert "1 steps remain" in llm.requests[-1][0][-1]["content"]


def test_same_turn_finish_calls_cannot_bypass_review_feedback() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(
                None, call("finish", summary="first", goal_met=True), call("finish", summary="second", goal_met=True)
            ),
            reply(None, call("finish", summary="reviewed; blocked", goal_met=False)),
        ]
    )
    events: list[str] = []
    result = Agent(
        llm, FakeToolBox(), max_steps=20, goal=True, review_before_finish=True, on_event=lambda k, **p: events.append(k)
    ).run("fix it")
    assert result.finished and result.steps == 3 and result.summary == "reviewed; blocked" and not result.goal_met
    assert events.count("review.requested") == 1 and events.count("finish") == 1


def test_review_does_not_reopen_a_run_forced_to_finish_after_stalling() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply("thinking"),
            reply("thinking"),
            reply("thinking"),
            reply(None, call("finish", summary="blocked")),
        ]
    )
    result = Agent(llm, FakeToolBox(), max_steps=20, review_before_finish=True).run("fix it")
    assert result.finished and result.steps == 2 and llm.requests[-1][1] == FORCE_FINISH


def test_review_state_resets_between_runs() -> None:
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
            reply(None, call("finish", summary="reviewed")),
        ]
        * 2
    )
    events: list[str] = []
    agent = Agent(llm, FakeToolBox(), max_steps=20, review_before_finish=True, on_event=lambda k, **p: events.append(k))
    assert agent.run("first").steps == 3
    assert agent.run("second").steps == 3
    assert events.count("review.requested") == 2
