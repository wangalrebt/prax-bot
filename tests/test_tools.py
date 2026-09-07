from __future__ import annotations

import pytest

from prax_bot.sandbox import Checkpoint, resolve_in_workdir
from prax_bot.tools import MAX_READ_LINES, ToolBox, apply_edit, clip, tool_specs


class FakeWorkspace:
    """In-memory stand-in for the sandbox workspace: files only, no shell."""

    workdir = "/work"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = {k: v.encode() for k, v in (files or {}).items()}
        self.checkpoints = [Checkpoint(0, "img-0", "baseline")]

    @property
    def current_step(self) -> int:
        return self.checkpoints[-1].step

    def read(self, path: str) -> bytes:
        resolve_in_workdir(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path: str, content: bytes) -> None:
        resolve_in_workdir(path)
        self.files[path] = content
        self.checkpoints.append(Checkpoint(len(self.checkpoints), f"img-{len(self.checkpoints)}", f"write: {path}"))

    def restore(self, step: int) -> Checkpoint:
        target = self.checkpoints[step]
        self.checkpoints.append(Checkpoint(len(self.checkpoints), target.image, f"restore: step {step}"))
        return target


def test_apply_edit_requires_exactly_one_match() -> None:
    text = "a\nb\na\n"
    assert apply_edit(text, "zzz", "y")[1].startswith("old text was not found")
    assert "occurs 2 times" in apply_edit(text, "a\n", "y")[1]
    new, err = apply_edit(text, "b\n", "c\n")
    assert err is None and new == "a\nc\na\n"
    assert apply_edit(text, "", "x")[1] == "old must not be empty"


def test_clip_keeps_head_and_tail() -> None:
    assert clip("short") == "short"
    long = "x" * 100 + "MIDDLE" + "y" * 100
    out = clip(long, limit=40)
    assert out.startswith("x" * 20) and out.endswith("y" * 20) and "omitted" in out


@pytest.mark.parametrize("path", [".", "src/x.py", "/work/src", "./a/../b"])
def test_resolve_in_workdir_accepts_repo_paths(path: str) -> None:
    assert resolve_in_workdir(path).startswith("/work")


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "/work/../root", "src/../../x"])
def test_resolve_in_workdir_rejects_escapes(path: str) -> None:
    with pytest.raises(ValueError):
        resolve_in_workdir(path)


def test_tool_specs_are_openai_function_schemas() -> None:
    specs = tool_specs()
    names = {s["function"]["name"] for s in specs}
    assert names == {"list_files", "search", "read_file", "outline", "edit_file", "write_file", "run", "todo_write", "restore", "finish"}
    for s in specs:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"
        assert s["function"]["description"]


def test_read_file_pages_and_numbers_lines() -> None:
    body = "\n".join(f"line {i}" for i in range(1, 1001))
    box = ToolBox(FakeWorkspace({"big.txt": body}))
    out = box.call("read_file", {"path": "big.txt"})
    assert out.startswith("big.txt lines 1-400 of 1000")
    assert "  1| line 1" in out and f"start_line={MAX_READ_LINES + 1}" in out
    out2 = box.call("read_file", '{"path": "big.txt", "start_line": 990}')
    assert "lines 990-1000 of 1000" in out2 and "1000| line 1000" in out2
    assert box.call("read_file", {"path": "big.txt", "start_line": 5000}).startswith("error:")
    assert box.call("read_file", {"path": "missing.txt"}).startswith("error: file not found")


def test_edit_and_write_go_through_the_workspace() -> None:
    ws = FakeWorkspace({"app.py": "def f():\n    return 1\n"})
    box = ToolBox(ws)
    assert box.call("edit_file", {"path": "app.py", "old": "return 1", "new": "return 2"}).startswith("error: read app.py before editing")
    box.call("read_file", {"path": "app.py"})
    out = box.call("edit_file", {"path": "app.py", "old": "return 1", "new": "return 2"})
    assert out.startswith("edited app.py") and "checkpoint 1" in out
    assert ws.files["app.py"] == b"def f():\n    return 2\n"
    assert box.call("edit_file", {"path": "app.py", "old": "nope", "new": "x"}).startswith("error: old text was not found")
    assert box.call("write_file", {"path": "app.py", "content": "x"}).startswith("error: app.py already exists")
    assert box.call("write_file", {"path": "new/mod.py", "content": "print(1)\n"}).startswith("wrote new/mod.py")
    assert ws.files["new/mod.py"] == b"print(1)\n"


def test_bad_arguments_and_unknown_tools_return_errors() -> None:
    box = ToolBox(FakeWorkspace())
    assert box.call("nope", {}).startswith("error: unknown tool")
    assert box.call("finish", {"summary": "x"}).startswith("error: unknown tool")
    assert box.call("read_file", "{not json").startswith("error: invalid arguments")
    assert box.call("read_file", {"start_line": 1}).startswith("error: invalid arguments")
    assert box.call("read_file", {"path": "../x"}).startswith("error: path")
    assert box.call("restore", {"step": 0}).startswith("restored the workspace to checkpoint 0")
