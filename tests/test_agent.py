from __future__ import annotations

import json
from types import SimpleNamespace

from prax_bot.agent import FORCE_FINISH, Agent, assistant_message, elide_old_tool_results
from prax_bot.llm import Usage


def call(name: str, **args: object) -> SimpleNamespace:
    return SimpleNamespace(id=f"call-{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content: str | None = None, *calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=list(calls))


class FakeLLM:
    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[list[dict], object]] = []
        self.usage = Usage()

    def complete(self, messages, tools, *, tool_choice="auto", temperature=0.2, model=None):
        self.requests.append(([dict(m) for m in messages], tool_choice))
        self.usage.calls += 1
        if not self.replies:
            return reply("nothing left to say")
        return self.replies.pop(0)


class FakeToolBox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call(self, name: str, raw_args: str) -> str:
        self.calls.append((name, raw_args))
        return f"ok {name}"


def test_tool_calls_are_dispatched_until_finish() -> None:
    llm = FakeLLM([
        reply(None, call("read_file", path="a.py")),
        reply("editing", call("edit_file", path="a.py", old="x", new="y"), call("run", command="pytest")),
        reply(None, call("finish", summary="done and tested")),
    ])
    box = FakeToolBox()
    events: list[str] = []
    result = Agent(llm, box, max_steps=10, on_event=lambda kind, **p: events.append(kind)).run("fix it")

    assert result.finished and result.summary == "done and tested" and result.steps == 3
    assert [c[0] for c in box.calls] == ["read_file", "edit_file", "run"]
    final_messages = llm.requests[-1][0]
    assert final_messages[0]["role"] == "system" and final_messages[1] == {"role": "user", "content": "fix it"}
    assert [m["role"] for m in final_messages[2:]] == ["assistant", "tool", "assistant", "tool", "tool"]
    assert final_messages[3]["tool_call_id"] == "call-read_file" and final_messages[3]["content"] == "ok read_file\n(step 1 of 10)"
    assert "finish" in events and events.count("tool.result") == 3


def test_prose_without_tools_is_nudged_then_forced() -> None:
    llm = FakeLLM([reply("Let me think."), reply("Still thinking."), reply("Hmm."), reply(None, call("finish", summary="gave up"))])
    result = Agent(llm, FakeToolBox(), max_steps=10).run("task")

    assert result.finished and result.summary == "gave up"
    assert [tc for _, tc in llm.requests] == ["auto", "auto", "auto", FORCE_FINISH]
    nudges = [m for m in llm.requests[3][0] if m["role"] == "user"][1:]
    assert len(nudges) == 3


def test_budget_forces_finish_on_last_step() -> None:
    llm = FakeLLM([reply(None, call("run", command="ls")), reply(None, call("run", command="ls")), reply("no tool")])
    result = Agent(llm, FakeToolBox(), max_steps=3).run("task")

    assert not result.finished and result.reason == "no finish call after forcing"
    assert llm.requests[2][1] == FORCE_FINISH


def test_step_budget_exhausted_when_model_keeps_calling_tools() -> None:
    llm = FakeLLM([reply(None, call("run", command="ls")) for _ in range(5)])
    result = Agent(llm, FakeToolBox(), max_steps=2).run("task")
    assert not result.finished and result.reason == "budget" and result.steps == 2


def test_assistant_message_is_rebuilt_without_extras() -> None:
    msg = SimpleNamespace(content="hi", reasoning_content="secret", tool_calls=[call("run", command="ls")])
    out = assistant_message(msg)
    assert out == {
        "role": "assistant",
        "content": "hi",
        "tool_calls": [{"id": "call-run", "type": "function", "function": {"name": "run", "arguments": '{"command": "ls"}'}}],
    }
    assert assistant_message(SimpleNamespace(content=None, tool_calls=None)) == {"role": "assistant", "content": ""}


def test_elision_keeps_recent_tool_outputs() -> None:
    messages = [{"role": "system", "content": "s"}]
    for i in range(10):
        messages.append({"role": "assistant", "content": ""})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 1000})
    elided = elide_old_tool_results(messages, max_chars=6500, keep_last=6)
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert elided == 4
    assert all(m["content"].startswith("[elided") for m in tool_msgs[:4])
    assert all(m["content"] == "x" * 1000 for m in tool_msgs[4:])
    assert elide_old_tool_results(messages, max_chars=10_000) == 0
