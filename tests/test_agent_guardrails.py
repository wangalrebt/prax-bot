"""Round 12: restore cannot wipe every repository edit; the model is told to wrap up late in the budget."""

from __future__ import annotations

import json

from prax_bot.core.agent import Agent
from prax_bot.core.tools import ToolBox
from tests.fakes import FakeLLM, FakeToolBox, FakeWorkspace, call, reply


def test_restore_refuses_to_discard_every_repository_edit() -> None:
    ws = FakeWorkspace({"a.py": "x\n", "b.py": "y\n"})
    box = ToolBox(ws)
    assert box.repo_edited is False
    box.call("read_file", json.dumps({"path": "a.py"}))
    assert box.call("edit_file", json.dumps({"path": "a.py", "old": "x", "new": "z"})).output.startswith(
        "edited"
    )  # checkpoint 1
    box.call(
        "write_file", json.dumps({"path": "/tmp/praxbot/scratch/t.py", "content": "print(1)\n"})
    )  # checkpoint 2, scratch
    assert box.repo_edited is True

    refused = box.call("restore", json.dumps({"step": 0})).output
    assert refused.startswith("error:") and "discard every change" in refused and "checkpoints 1" in refused
    assert ws.current_step == 2, "nothing was restored"

    assert box.call("restore", json.dumps({"step": 1})).output.startswith("restored")  # keeps the edit; checkpoint 3
    box.call("read_file", json.dumps({"path": "b.py"}))
    assert box.call("edit_file", json.dumps({"path": "b.py", "old": "y", "new": "w"})).output.startswith(
        "edited"
    )  # checkpoint 4
    assert box.call("restore", json.dumps({"step": 2})).output.startswith("restored"), (
        "one edit survives, so this is allowed"
    )
    assert "discard every change" in box.call("restore", json.dumps({"step": 0})).output, (
        "the surviving edit is still tracked"
    )


def test_late_budget_nudge_tells_the_model_to_wrap_up() -> None:
    llm = FakeLLM(
        [
            reply(None, call("read_file", path="a.py")),
            reply(None, call("read_file", path="b.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
        ]
    )
    result = Agent(llm, FakeToolBox(), max_steps=5).run("fix it")
    assert result.finished and result.steps == 4
    tool_results = [m["content"] for m in llm.requests[-1][0] if m["role"] == "tool"]
    assert "Only" not in tool_results[0], "step 1 of 5 is not late"
    assert "no repository file has been changed" in tool_results[1] and "Only 3 steps remain" in tool_results[1]
    assert "Then call finish" in tool_results[2] and "no repository file" not in tool_results[2]
    assert "If no edit is needed" in tool_results[1]
    assert "Scheduled runtime checks can remain pending" in tool_results[2]


def test_halfway_nudge_fires_once_when_nothing_is_edited() -> None:
    reads = [reply(None, call("read_file", path=f"{i}.py")) for i in range(6)]
    llm = FakeLLM(
        [
            *reads,
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="done")),
        ]
    )
    result = Agent(llm, FakeToolBox(), max_steps=10).run("fix it")
    assert result.finished and result.steps == 8
    tool_results = [m["content"] for m in llm.requests[-1][0] if m["role"] == "tool"]
    assert "Half of the budget" not in tool_results[3], "step 4 of 10 is not halfway"
    assert "Half of the budget" in tool_results[4] and "5 steps remain" in tool_results[4]
    assert "Half of the budget" not in tool_results[5], "the halfway nudge is sent once"
    assert "For read-only or already satisfied tasks, finish" in tool_results[4]
    assert "3 steps remain." in tool_results[6] and "Then call finish" in tool_results[6]
    # nothing at halfway when an edit has already been made
    llm = FakeLLM(
        [
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            *reads[:5],
            reply(None, call("finish", summary="done")),
        ]
    )
    Agent(llm, FakeToolBox(), max_steps=10).run("fix it")
    tool_results = [m["content"] for m in llm.requests[-1][0] if m["role"] == "tool"]
    assert not any("Half of the budget" in r for r in tool_results)


def test_protected_message_requires_evidence_before_dismissing_a_failure() -> None:
    ws = FakeWorkspace({"tests/test_a.py": "assert old\n", "a.py": "old\n"})
    box = ToolBox(ws, protected=("tests/*",))
    box.call("read_file", json.dumps({"path": "tests/test_a.py"}))
    out = box.call("edit_file", json.dumps({"path": "tests/test_a.py", "old": "old", "new": "new"})).output
    assert out.startswith("error:") and "an explicit requirement must contradict it" in out
    assert "otherwise treat it as unresolved" in out
    assert "Do not discard a supported fix because an unrelated check failed" in out
    assert ws.read("tests/test_a.py") == b"assert old\n"
