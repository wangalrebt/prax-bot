"""Round 12: restore cannot wipe every repository edit; the model is told to wrap up late in the budget."""

from __future__ import annotations

import json

from prax_bot.agent import Agent
from prax_bot.tools import ToolBox
from tests.test_agent import FakeLLM, FakeToolBox, call, reply
from tests.test_tools import FakeWorkspace


def test_restore_refuses_to_discard_every_repository_edit() -> None:
    ws = FakeWorkspace({"a.py": "x\n", "b.py": "y\n"})
    box = ToolBox(ws)
    assert box.repo_edited is False
    box.call("read_file", json.dumps({"path": "a.py"}))
    assert box.call("edit_file", json.dumps({"path": "a.py", "old": "x", "new": "z"})).startswith(
        "edited"
    )  # checkpoint 1
    box.call(
        "write_file", json.dumps({"path": "/tmp/praxbot/scratch/t.py", "content": "print(1)\n"})
    )  # checkpoint 2, scratch
    assert box.repo_edited is True

    refused = box.call("restore", json.dumps({"step": 0}))
    assert refused.startswith("error:") and "discard every change" in refused and "checkpoints 1" in refused
    assert ws.current_step == 2, "nothing was restored"

    assert box.call("restore", json.dumps({"step": 1})).startswith("restored")  # keeps the edit; checkpoint 3
    box.call("read_file", json.dumps({"path": "b.py"}))
    assert box.call("edit_file", json.dumps({"path": "b.py", "old": "y", "new": "w"})).startswith(
        "edited"
    )  # checkpoint 4
    assert box.call("restore", json.dumps({"step": 2})).startswith("restored"), "one edit survives, so this is allowed"
    assert "discard every change" in box.call("restore", json.dumps({"step": 0})), "the surviving edit is still tracked"


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
    assert "Wrap up" in tool_results[2] and "no repository file" not in tool_results[2]


def test_protected_message_says_not_to_revert_for_stale_tests() -> None:
    ws = FakeWorkspace({"tests/test_a.py": "assert old\n", "a.py": "old\n"})
    box = ToolBox(ws, protected=("tests/*",))
    box.call("read_file", json.dumps({"path": "tests/test_a.py"}))
    out = box.call("edit_file", json.dumps({"path": "tests/test_a.py", "old": "old", "new": "new"}))
    assert out.startswith("error:") and "keep your fix" in out and "Do not revert" in out
