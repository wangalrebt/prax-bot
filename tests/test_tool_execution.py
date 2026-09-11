from __future__ import annotations

from prax_bot.core.editing import apply_edit
from prax_bot.core.tools import OUTPUT_TAIL_BYTES, ToolBox
from prax_bot.workspace.types import CommandResult
from tests.fakes import FakeWorkspace


class CaptureRun(FakeWorkspace):
    def __init__(self, files=None, stdout="", exit_code=0):
        super().__init__(files)
        self.commands: list[str] = []
        self.stdout, self.exit_code = stdout, exit_code

    def run(self, command, *, timeout=None, mutate=True, truncate_output_at=None):
        self.commands.append(command)
        return CommandResult(
            command, self.exit_code, self.stdout, "", 0.1, False, False, "img" if mutate else None, 0.0
        )


def test_run_spills_full_output_and_reports_the_tail() -> None:
    ws = CaptureRun(stdout="last lines of a long run\n__praxbot_bytes=123456\n")
    out = ToolBox(ws).call("run", {"command": "pytest -q", "workdir": "src"}).output
    cmd = ws.commands[0]
    assert (
        "cd src && { pytest -q" in cmd
        and "> ${TMPDIR:-/tmp}/praxbot/out-1.log 2>&1" in cmd
        and f"tail -c {OUTPUT_TAIL_BYTES}" in cmd
    )
    assert "last lines of a long run" in out and "__praxbot_bytes" not in out
    assert (
        f"showing the last {OUTPUT_TAIL_BYTES:,} of 123,456 bytes; the full output is in ${{TMPDIR:-/tmp}}/praxbot/out-1.log"
        in out
    )
    assert ToolBox(ws).call("run", {"command": "ls", "workdir": "../x"}).output.startswith("error: path")


def test_failed_shell_command_has_an_explicit_error_and_keeps_its_output() -> None:
    result = ToolBox(CaptureRun(stdout="failure details", exit_code=3)).call("run", {"command": "failing"})
    assert result.error and result.output.startswith("exit code 3")
    assert "failure details" in result.output


def test_edits_require_a_fresh_read_after_commands_or_restores() -> None:
    ws = CaptureRun({"a.py": "x = 1\n"}, stdout="ok\n__praxbot_bytes=3\n")
    box = ToolBox(ws)
    box.call("read_file", {"path": "a.py"})
    assert box.call("edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"}).output.startswith("edited a.py")
    # its own edit keeps the file fresh
    assert box.call("edit_file", {"path": "a.py", "old": "x = 2", "new": "x = 3"}).output.startswith("edited a.py")
    box.call("run", {"command": "pytest"})  # a command that changes nothing leaves the file fresh
    assert box.call("edit_file", {"path": "a.py", "old": "x = 3", "new": "x = 4"}).output.startswith("edited a.py")
    ws.files["a.py"] = b"x = 4  # formatted\n"  # something else changed the file
    assert (
        "It has changed since you last read it"
        in box.call("edit_file", {"path": "a.py", "old": "x = 4", "new": "x = 5"}).output
    )
    box.call("read_file", {"path": "a.py"})
    assert box.call("edit_file", {"path": "a.py", "old": "x = 4", "new": "x = 5"}).output.startswith("edited a.py")


def test_write_file_marks_the_file_observed() -> None:
    box = ToolBox(CaptureRun({}))
    box.call("write_file", {"path": "new.py", "content": "a = 1\n"})
    assert box.call("edit_file", {"path": "new.py", "old": "a = 1", "new": "a = 2"}).output.startswith("edited new.py")


def test_replace_all() -> None:
    assert apply_edit("a b a\n", "a", "c", replace_all=True) == ("c b c\n", None)
    assert apply_edit("a b a\n", "a", "c")[1].startswith("old text occurs 2 times")
    assert apply_edit("a b\n", "z", "c", replace_all=True)[1].startswith("old text was not found")


def test_todo_write_records_and_summarises() -> None:
    box = ToolBox(CaptureRun({}))
    out = box.call(
        "todo_write",
        {
            "todos": [
                {"content": "read tests", "status": "completed"},
                {"content": "fix bug", "status": "in_progress"},
                {"content": "run suite", "status": "pending"},
            ]
        },
    ).output
    assert out.startswith("todo list recorded: 1 done, 1 in progress, 1 pending")
    assert "[x] read tests" in out and "[>] fix bug" in out and "[ ] run suite" in out and len(box.todos) == 3
    assert box.call("todo_write", {"todos": [{"content": "x", "status": "later"}]}).output.startswith(
        "error: invalid arguments"
    )
