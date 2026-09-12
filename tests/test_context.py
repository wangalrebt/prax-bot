"""Context management: real context size per call, pruning of tool results by age, and summarising compaction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from prax_bot.core.agent import Agent
from prax_bot.core.context import (
    COMPACT_KEEP_TURNS,
    COMPACT_MIN_GAP,
    KEEP_FULL,
    compact_transcript,
    prune_by_age,
    render_for_summary,
)
from prax_bot.core.llm import LLM
from prax_bot.settings import Settings
from tests.fakes import FakeClient, FakeLLM, FakeToolBox, call, reply
from tests.helpers import settings as base_settings


def tool_messages(n: int, size: int) -> list[dict]:
    """A transcript of n tool exchanges, each result `size` characters, oldest first."""
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(n):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + ("x" * size)})
    return messages


def test_prune_by_age_keeps_recent_whole_and_shrinks_older_in_tiers() -> None:
    messages = tool_messages(30, 10_000)
    names = {f"c{i}": "read_file" for i in range(30)}
    changed = prune_by_age(messages, names=names, compact=lambda name, out: f"[compacted] {name}: {out[:12]}")
    results = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(len(r) > 10_000 for r in results[-KEEP_FULL:]), "the newest six are untouched"
    assert all(len(r) <= 4096 and "pruned" in r for r in results[-12:-KEEP_FULL]), "then cut to about 4k"
    assert all(len(r) <= 1024 and "pruned" in r for r in results[-24:-12]), "then to about 1k"
    assert all(r.startswith("[compacted] read_file") for r in results[:-24]), "the oldest become digests"
    assert changed == 24
    assert prune_by_age(messages, names=names, compact=lambda n, o: "[compacted] again") == 0, (
        "a second pass changes nothing"
    )


def test_compact_transcript_keeps_shape_and_drops_the_repo_map() -> None:
    messages = tool_messages(10, 200)
    messages.insert(2, {"role": "user", "content": "Repository map, definitions with line numbers.\nfoo.py..."})
    messages.insert(3, {"role": "user", "content": "Project instructions from AGENTS.md:\nuse tabs"})
    llm = BigContextLLM([reply("Task: t. Learned: foo.py:12 is the bug. Changed: nothing yet. Remains: fix it.")])
    new, summary, reason = compact_transcript(messages, llm, keep_turns=3)
    assert summary.startswith("Task: t.") and reason == ""
    roles = [m["role"] for m in new]
    assert roles[:3] == ["system", "user", "user"] and "Repository map" not in new[2]["content"], (
        "instructions kept, map dropped"
    )
    assert (
        new[3]["role"] == "user" and "compacted to save context" in new[3]["content"] and summary in new[3]["content"]
    )
    tail = new[4:]
    assert (
        tail[0]["role"] == "assistant"
        and tail[1]["role"] == "tool"
        and tail[1]["tool_call_id"] == tail[0]["tool_calls"][0]["id"]
    )
    assert sum(1 for m in tail if m["role"] == "assistant") == 3, "the last three turns are verbatim"
    assert llm.tools_seen == [None], "the summary call carries no tools"
    request = llm.requests[0][0]
    assert "call read_file" in request[1]["content"] and "result 0" in request[1]["content"]


def test_compact_transcript_is_a_no_op_when_short_or_when_the_model_gives_nothing() -> None:
    short = tool_messages(2, 50)
    assert compact_transcript(short, FakeLLM([reply("summary")]))[1:] == ("", "too short")
    long = tool_messages(10, 50)
    same, summary, reason = compact_transcript(long, FakeLLM([reply("")]))
    assert summary == "" and reason == "no summary" and same is long


class BigContextLLM(FakeLLM):
    """A fake whose last call always looks larger than the budget, and that records the tools each call carried."""

    last_prompt_tokens = 500_000

    def __init__(self, replies):
        super().__init__(replies)
        self.tools_seen: list = []

    def complete(self, messages, tools, **kw):
        self.tools_seen.append(tools)
        return super().complete(messages, tools, **kw)


def test_agent_compacts_once_the_context_passes_the_budget() -> None:
    # compaction needs more than the kept turns to exist: four steps first, then the summary is asked for
    llm = BigContextLLM(
        [
            *[reply(None, call("read_file", path=f"f{i}.py")) for i in range(4)],  # steps 1-4
            reply("Task: fix. Learned: f0.py. Changed: none. Remains: edit."),  # the summary, at the top of step 5
            reply(None, call("edit_file", path="a.py", old="x", new="y")),  # step 5
            reply(None, call("finish", summary="done")),
        ]
    )
    events: list[dict] = []
    result = Agent(
        llm,
        FakeToolBox(),
        max_steps=10,
        context_budget=80_000,
        verification_commands=(("tests", "pytest"),),
        on_event=lambda k, **p: events.append({"kind": k, **p}),
    ).run("fix")
    assert result.finished
    compactions = [e for e in events if e["kind"] == "context.compacted"]
    assert len(compactions) == 1 and compactions[0]["step"] == 4 and compactions[0]["tokens_before"] == 500_000
    assert compactions[0]["summary_chars"] > 0 and llm.tools_seen[4] is None
    final_messages = llm.requests[-1][0]
    assert final_messages[0] == llm.requests[0][0][0], "the full runtime instructions survive compaction"
    assert "- tests: pytest" in final_messages[0]["content"]
    assert "remote Linux sandbox" in final_messages[0]["content"]
    assert any("compacted to save context" in str(m.get("content")) for m in final_messages)
    assert next(e["context"] for e in events if e["kind"] == "llm.call") == 500_000


def test_agent_waits_for_enough_turns_and_then_for_the_gap() -> None:
    first = COMPACT_KEEP_TURNS + 1  # the first step at which more than the kept turns exist
    second = first + COMPACT_MIN_GAP

    def read(i: int):
        return reply(None, call("read_file", path=f"f{i}.py"))

    replies = [read(i) for i in range(first)] + [reply("summary one")]
    replies += [read(i) for i in range(first, second)] + [reply("summary two")]
    replies += [read(second), reply(None, call("finish", summary="ok"))]
    llm = BigContextLLM(replies)
    events: list[dict] = []
    Agent(
        llm, FakeToolBox(), max_steps=30, context_budget=1, on_event=lambda k, **p: events.append({"kind": k, **p})
    ).run("go")
    steps = [e["step"] for e in events if e["kind"] == "context.compacted"]
    assert steps == [first, second], "not before the kept turns exist, then not again until the gap has passed"


def test_llm_sends_no_tool_fields_without_tools_and_records_context(tmp_path: Path) -> None:
    client = FakeClient()
    llm = LLM(base_settings(tmp_path), client=client)
    llm.complete([{"role": "user", "content": "summarise"}], None)
    assert "tools" not in client.calls[0] and "tool_choice" not in client.calls[0]
    assert llm.last_prompt_tokens == 3 and llm.last_completion_tokens == 1
    llm.complete([], [{"type": "function", "function": {"name": "x", "parameters": {}}}])
    assert client.calls[1]["tools"] and client.calls[1]["tool_choice"] == "auto"


def test_context_budget_is_a_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PRAXBOT_HOME", str(home))
    monkeypatch.delenv("PRAXBOT_CONTEXT_BUDGET", raising=False)
    assert Settings.from_env().context_budget == 80_000
    (home / "config.toml").write_text("context_budget = 50000\n")
    assert Settings.from_env().context_budget == 50_000
    monkeypatch.setenv("PRAXBOT_CONTEXT_BUDGET", "60000")
    assert Settings.from_env().context_budget == 60_000
    assert Settings.from_env(context_budget=70_000).context_budget == 70_000


def test_render_for_summary_reads_both_dict_and_object_tool_calls() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [SimpleNamespace(function=SimpleNamespace(name="run", arguments='{"command": "pytest"}'))],
        },
        {"role": "tool", "tool_call_id": "1", "content": "exit code 0\nall good"},
    ]
    text = render_for_summary(messages)
    assert text.startswith("user: do it\nassistant: thinking | call run(") and "result: exit code 0 all good" in text
