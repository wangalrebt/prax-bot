"""read_file(symbol=...) returns one definition whole, in Python by parsing and elsewhere by indentation."""

from __future__ import annotations

import json

from prax_bot.core.navigation import definition_span
from prax_bot.core.tools import ToolBox, tool_specs
from tests.fakes import FakeWorkspace

PY_SOURCE = '''import os


@decorated
class Table:
    """A table."""

    def render(self):
        return 1

    def pad(self, width):
        return width


def pad(s):
    return s


async def fetch():
    pass
'''

JS_SOURCE = """function a() {
  return 1;
}

class Widget {
  draw() {}
}

function c() {
  x();
}
"""


def test_definition_span_in_python_covers_decorators_methods_and_ambiguity() -> None:
    assert definition_span(PY_SOURCE, "Table") == (4, 12)
    assert definition_span(PY_SOURCE, "Table.render") == (8, 9)
    assert definition_span(PY_SOURCE, "fetch") == (19, 20)
    assert definition_span(PY_SOURCE, "render") == (8, 9), "an unambiguous method is found by its bare name"
    assert definition_span(PY_SOURCE, "pad") == (15, 16), "the exact top-level name wins over the method"
    assert definition_span(PY_SOURCE, "Table.pad") == (11, 12)
    assert definition_span(PY_SOURCE, "nope") == []
    assert definition_span(PY_SOURCE, "tab") == ["Table"], "a partial name suggests candidates"


def test_definition_span_falls_back_to_indentation_for_other_languages() -> None:
    assert definition_span(JS_SOURCE, "a") == (1, 3)
    assert definition_span(JS_SOURCE, "Widget") == (5, 7)
    assert definition_span(JS_SOURCE, "c") == (9, 11)
    assert definition_span(JS_SOURCE, "zz") == []


def test_read_file_by_symbol_returns_the_definition_and_explains_misses() -> None:
    box = ToolBox(FakeWorkspace({"t.py": PY_SOURCE}))
    out = box.call("read_file", json.dumps({"path": "t.py", "symbol": "Table.render"})).output
    assert out.startswith("t.py lines 8-9 of 20 (Table.render)\n")
    assert "8|" in out and "def render" in out and "def pad" not in out
    out = box.call("read_file", json.dumps({"path": "t.py", "symbol": "Tab"})).output
    assert out.startswith("error: no definition named 'Tab' in t.py; did you mean Table")
    out = box.call("read_file", json.dumps({"path": "t.py", "symbol": "zzz"})).output
    assert out == "error: no definition named 'zzz' in t.py; call outline to see what it defines"
    spec = next(s for s in tool_specs() if s["function"]["name"] == "read_file")
    assert "symbol" in spec["function"]["parameters"]["properties"]
    assert "symbol" in spec["function"]["description"]
