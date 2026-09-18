"""Context management: real context size per call, tool results and call arguments that age out, the repository map
that goes once editing has started, and summarising compaction."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from prax_bot.core.agent import Agent
from prax_bot.core.context import (
    ARGS_KEEP_CALLS,
    COMPACT_KEEP_TURNS,
    COMPACT_MIN_GAP,
    ELIDED_MARK,
    OTHER_POLICY,
    READ_POLICY,
    REPO_MAP_STEPS,
    compact_transcript,
    prune_by_age,
    render_for_summary,
    stub_old_call_arguments,
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


@pytest.mark.parametrize(("tool", "policy"), [("read_file", READ_POLICY), ("run", OTHER_POLICY)])
def test_prune_by_age_keeps_recent_whole_and_shrinks_older_in_tiers(tool: str, policy: tuple) -> None:
    keep_full, ((first_limit, first_cap), (last_limit, last_cap)) = policy
    messages = tool_messages(30, 10_000)
    names = {f"c{i}": tool for i in range(30)}
    changed = prune_by_age(messages, names=names, compact=lambda name, out: f"[compacted] {name}: {out[:12]}")
    results = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(len(r) > 10_000 for r in results[-keep_full:]), "the newest are untouched"
    assert all(len(r) <= first_cap and "pruned" in r for r in results[-first_limit:-keep_full]), "then cut once"
    assert all(len(r) <= last_cap and "pruned" in r for r in results[-last_limit:-first_limit]), "then cut harder"
    assert all(r.startswith(f"[compacted] {tool}") for r in results[:-last_limit]), "the oldest become digests"
    assert changed == 30 - keep_full
    assert prune_by_age(messages, names=names, compact=lambda n, o: "[compacted] again") == 0, (
        "a second pass changes nothing"
    )


def test_file_reads_outlive_command_output_of_the_same_age() -> None:
    messages = tool_messages(8, 10_000)
    names = {f"c{i}": "read_file" if i % 2 else "run" for i in range(8)}  # oldest first: run, read, run, read, ...
    prune_by_age(messages, names=names)
    results = [m["content"] for m in messages if m["role"] == "tool"]
    reads, runs = results[1::2], results[0::2]
    assert all(len(r) > 10_000 for r in reads[-3:]), "reads up to five results back are still whole"
    assert len(runs[-1]) > 10_000 and all("pruned" in r for r in runs[:-2]), "command output is cut after three"


def command_result(passed: int = 120) -> str:
    """A test run as the run tool returns it: what the command printed, then what the harness appends."""
    printed = [f"tests/test_urls.py::test_case_{i} PASSED" for i in range(passed)]
    printed += [
        "FAILED tests/test_urls.py::test_bad_label - AssertionError",
        "==== 8 failed, 27 passed, 7 errors in 0.55s ====",
    ]
    return "\n".join(
        [
            "exit code 1 · 0.8s · checkpoint 17",
            "output:",
            *printed,
            "(showing the last 20,000 of 23,242 bytes; the full output is in /tmp/praxbot/out-13.log, grep or sed it with run)",
            "code referenced above:",
            *[
                f"  requests/models.py:{300 + i} is inside def prepare_url(self, url, params): (line 360)"
                for i in range(8)
            ],
            "note: 3 tests fail before any edit; failures in this list later are not caused by your changes: a, b, c",
            "(step 31 of 50)",
        ]
    )


@pytest.mark.parametrize("age", [OTHER_POLICY[0], OTHER_POLICY[1][0][0]])  # the first tier, then the second
def test_an_old_command_result_still_says_how_the_command_ended(age: int) -> None:
    messages = tool_messages(age + 1, 50)
    oldest = next(m for m in messages if m["role"] == "tool")
    oldest["content"] = command_result()
    names = {f"c{i}": "run" for i in range(age + 1)}
    assert prune_by_age(messages, names=names) >= 1
    kept = oldest["content"]
    assert "8 failed, 27 passed, 7 errors" in kept, "the line a finish decision rests on"
    assert "out-13.log" in kept and "note: 3 tests fail before any edit" in kept and kept.endswith("(step 31 of 50)")
    assert "is inside def" not in kept, "the definitions it referred to were for the step that followed"
    assert len(kept) < len(command_result()) // 3
    assert prune_by_age(messages, names=names) == 0, "a second pass changes nothing"


def write_call(i: int, size: int) -> dict:
    arguments = json.dumps({"path": f"scratch/repro_{i}.py", "content": "x" * size})
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": f"w{i}", "type": "function", "function": {"name": "write_file", "arguments": arguments}}],
    }


def test_old_call_arguments_become_their_size_and_recent_ones_stay() -> None:
    messages = [write_call(i, 1_000) for i in range(ARGS_KEEP_CALLS + 3)]
    messages.insert(1, write_call(99, 20))  # short arguments are left alone at any age
    broken = write_call(98, 1_000)
    broken["tool_calls"][0]["function"]["arguments"] = "{not json " + "x" * 500
    messages.insert(2, broken)
    assert stub_old_call_arguments(messages) == 3
    arguments = [json.loads(m["tool_calls"][0]["function"]["arguments"]) for m in messages if m is not broken]
    old = [a for a in arguments if a["content"].startswith(ELIDED_MARK)]
    assert [a["path"] for a in old] == ["scratch/repro_0.py", "scratch/repro_1.py", "scratch/repro_2.py"]
    assert all("1,000 characters" in a["content"] for a in old), "the placeholder says what was there"
    assert arguments[1] == {"path": "scratch/repro_99.py", "content": "x" * 20}
    assert broken["tool_calls"][0]["function"]["arguments"].startswith("{not json"), "unparseable arguments stay"
    assert all(len(a["content"]) == 1_000 for a in arguments[-ARGS_KEEP_CALLS:]), "the newest calls are verbatim"
    assert stub_old_call_arguments(messages) == 0, "a second pass changes nothing"


class MapToolBox(FakeToolBox):
    def repo_map(self, task: str) -> str:
        return "pkg/core.py\n  12: def resolve"


def test_the_repository_map_goes_once_the_repository_is_edited_and_the_first_steps_have_passed() -> None:
    reads = [reply(None, call("read_file", path=f"f{i}.py")) for i in range(REPO_MAP_STEPS + 1)]
    llm = FakeLLM(
        [
            *reads,
            reply(None, call("edit_file", path="pkg/core.py", old="a", new="b")),
            reply(None, call("finish", summary="ok")),
        ]
    )
    events: list[dict] = []
    Agent(llm, MapToolBox(), max_steps=30, on_event=lambda k, **p: events.append({"kind": k, **p})).run("fix resolve")
    carried = ["12: def resolve" in str(messages[2]["content"]) for messages, _ in llm.requests]
    assert all(carried[: REPO_MAP_STEPS + 2]), "past the first steps the map stays while nothing has been edited"
    assert carried[REPO_MAP_STEPS + 2 :] == [False], "the call after the edit no longer carries it"
    final = llm.requests[-1][0][2]["content"]
    assert final.startswith("Repository map: removed"), (
        "a note stands in its place, and compaction drops it like the map"
    )
    assert [e["step"] for e in events if e["kind"] == "context.map_dropped"] == [REPO_MAP_STEPS + 2]


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
    assert llm.last_prompt_tokens == 3 and llm.usage.completion_tokens == 1
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
