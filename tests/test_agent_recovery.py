from __future__ import annotations

from prax_bot.core.agent import FORCE_FINISH, MAX_NUDGES, Agent
from tests.fakes import FakeLLM, FakeToolBox, call, reply


def test_blank_replies_are_retried_before_counting_as_stalls() -> None:
    llm = FakeLLM(
        [
            reply(""),
            reply(None),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
        ]
    )
    events: list[tuple[str, dict]] = []
    result = Agent(llm, FakeToolBox(), max_steps=10, on_event=lambda k, **p: events.append((k, p))).run("t")
    assert result.finished and result.steps == 3
    reasons = [p["reason"] for k, p in events if k == "nudge"]
    assert len(reasons) == 2 and all(r.startswith("blank reply") for r in reasons)
    # blank replies were not appended to the transcript
    assert not any(
        m["role"] == "assistant" and m["content"] == "" and "tool_calls" not in m for m in llm.requests[-1][0]
    )
    assert all(tc == "auto" for _, tc in llm.requests[:3])


def test_finish_is_forced_only_after_three_prose_stalls() -> None:
    llm = FakeLLM([reply("a"), reply("b"), reply("c"), reply(None, call("finish", summary="s"))])
    result = Agent(llm, FakeToolBox(), max_steps=10).run("t")
    assert result.finished
    assert [tc for _, tc in llm.requests] == ["auto"] * MAX_NUDGES + [FORCE_FINISH]


def test_forced_finish_with_junk_arguments_lets_the_model_continue() -> None:
    llm = FakeLLM(
        [
            reply("a"),
            reply("b"),
            reply("c"),
            reply(None, call("finish", pattern=":kbd:", path="sphinx/tests")),  # forced finish, but it wanted to search
            reply(None, call("search", pattern=":kbd:")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="fixed")),
        ]
    )
    box = FakeToolBox()
    result = Agent(llm, box, max_steps=10).run("t")
    assert result.finished and result.summary == "fixed"
    assert [c[0] for c in box.calls] == ["search", "edit_file"]
    assert llm.requests[3][1] == FORCE_FINISH and llm.requests[4][1] == "auto"
