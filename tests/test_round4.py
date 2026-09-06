from __future__ import annotations

from prax_bot.agent import REPEAT_NUDGE, Agent
from prax_bot.tools import ToolBox, tool_specs
from tests.test_agent import FakeLLM, FakeToolBox, call, reply
from tests.test_review_fixes import ShellFakeWorkspace


def test_outline_lists_definitions_with_line_numbers() -> None:
    ws = ShellFakeWorkspace({"big.py": "x"}, results=[(0, "big.py:12:def _padleft(width, s):\nbig.py:40:class Table:\n", "", False)])
    out = ToolBox(ws).call("outline", {"path": "big.py"})
    assert out == "big.py: 2 definitions\n12:def _padleft(width, s):\n40:class Table:"
    assert "grep -nHE" in ws.commands[0] and ws.commands[0].endswith("-- big.py")
    assert "outline" in {s["function"]["name"] for s in tool_specs()}


def test_search_no_match_hint_and_ignore_case() -> None:
    ws = ShellFakeWorkspace(results=[(1, "", "", False), (0, "a.py:3:Width = 1\n", "", False)])
    box = ToolBox(ws)
    assert box.call("search", {"pattern": "def _col_widths"}).startswith("no matches. Do not guess names")
    assert box.call("search", {"pattern": "width", "ignore_case": True}) == "a.py:3:Width = 1"
    assert " -rnIi " in ws.commands[1] and " -rnI " in ws.commands[0]


def test_repeated_call_is_caught_even_when_interleaved() -> None:
    same = lambda: reply(None, call("search", pattern="colwidths ="))  # noqa: E731
    other = lambda p: reply(None, call("read_file", path=p))  # noqa: E731
    llm = FakeLLM([same(), other("a.py"), same(), other("b.py"), same(), reply(None, call("finish", summary="s"))])
    events: list[tuple[str, dict]] = []
    Agent(llm, FakeToolBox(), max_steps=20, on_event=lambda k, **p: events.append((k, p))).run("t")
    nudges = [p for k, p in events if k == "nudge"]
    assert len(nudges) == 1 and nudges[0]["step"] == 5
    assert any(m.get("content") == REPEAT_NUDGE for m in llm.requests[-1][0])


def test_call_signature_ignores_key_order_and_defaults() -> None:
    from prax_bot.agent import call_signature

    a = call_signature("search", '{"pattern": "colwidths =", "path": "."}')
    b = call_signature("search", '{"path": ".", "pattern": "colwidths =", "max_results": 50, "ignore_case": false}')
    c = call_signature("search", '{"pattern": "colwidths =", "path": "tabulate"}')
    assert a == b and a != c
    assert call_signature("nope", "{bad").startswith("nope:")
