from __future__ import annotations

from prax_bot.core.tools import MAX_RESTORES, ToolBox, pytest_failures
from prax_bot.workspace.sandbox import CommandResult
from tests.test_tools import FakeWorkspace


class RunFake(FakeWorkspace):
    def __init__(self, outputs: list[str]):
        super().__init__({"a.py": "x = 1\n"})
        self.outputs = list(outputs)

    def run(self, command, *, timeout=None, mutate=True, truncate_output_at=None):
        out = self.outputs.pop(0) if self.outputs else ""
        return CommandResult(
            command, 1 if "FAILED" in out else 0, out, "", 0.1, False, False, "img" if mutate else None, 0.0
        )


def test_pytest_failures_parses_ids() -> None:
    out = "FAILED tests/test_a.py::test_x - assert 1\nERROR tests/test_b.py::test_y\nFAILED tests/test_a.py::test_x - again\n1 failed"
    assert pytest_failures(out) == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]


def test_preexisting_failures_are_pointed_out() -> None:
    first = "FAILED t/test_net.py::test_http - ConnectionError\nFAILED t/test_a.py::test_bug\n2 failed, 10 passed"
    later = "FAILED t/test_net.py::test_http - ConnectionError\n1 failed, 11 passed"
    box = ToolBox(RunFake([first, later]))
    out1 = box.call("run", {"command": "pytest"})
    assert "note: 2 tests fail before any edit" in out1 and "t/test_net.py::test_http" in out1
    box.call("read_file", {"path": "a.py"})
    box.call("edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"})
    out2 = box.call("run", {"command": "pytest"})
    assert "1 of these failures were already failing before your edits: t/test_net.py::test_http" in out2
    assert "now passing that failed before: t/test_a.py::test_bug" in out2


def test_first_test_run_after_an_edit_is_not_treated_as_baseline() -> None:
    box = ToolBox(RunFake(["FAILED t/test_a.py::test_bug\n1 failed"]))
    box.call("read_file", {"path": "a.py"})
    box.call("edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"})
    out = box.call("run", {"command": "pytest"})
    assert "note:" not in out


def test_restore_is_capped() -> None:
    box = ToolBox(FakeWorkspace({"a.py": "x"}))
    for _ in range(MAX_RESTORES):
        assert box.call("restore", {"step": 0}).startswith("restored")
    assert box.call("restore", {"step": 0}).startswith("error: restore limit")
