from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from contree_sdk.sdk.exceptions import ContreeError

from prax_bot.agent import REPEAT_NUDGE, Agent
from prax_bot.local import LocalWorkspace
from prax_bot.sandbox import CommandResult, suspicious_files
from prax_bot.session import json_safe
from prax_bot.tools import ToolBox
from tests.test_agent import FakeLLM, FakeToolBox, call, reply
from tests.test_local import make_repo
from tests.test_tools import FakeWorkspace


class ShellFakeWorkspace(FakeWorkspace):
    """FakeWorkspace plus a scripted `run`, so list/search error paths can be exercised."""

    def __init__(self, files=None, *, results=None):
        super().__init__(files)
        self.results = list(results or [])
        self.commands: list[str] = []

    def run(self, command, *, timeout=None, mutate=True, truncate_output_at=None):
        self.commands.append(command)
        exit_code, stdout, stderr, truncated = self.results.pop(0)
        return CommandResult(command, exit_code, stdout, stderr, 0.01, truncated, False, None, 0.0)


def test_backend_errors_become_tool_errors_instead_of_crashing() -> None:
    class Broken(FakeWorkspace):
        def read(self, path):
            raise ContreeError.__new__(ContreeError)

    out = ToolBox(Broken()).call("read_file", {"path": "x.py"})
    assert out.startswith("error: the workspace backend failed (ContreeError")


def test_reading_a_directory_is_reported_as_such(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "current").mkdir(parents=True)
    make_repo(root / "current")
    (root / "current" / "pkg").mkdir()
    out = ToolBox(LocalWorkspace(root, command_timeout=5)).call("read_file", {"path": "pkg"})
    assert out == "error: pkg is a directory, not a file"


def test_list_and_search_handle_empty_paths_errors_and_limits() -> None:
    ws = ShellFakeWorkspace(
        results=[
            (0, "a.py\nb.py\nc.py\n", "", False),
            (2, "", "grep: Unmatched ( or \\(", False),  # -E attempt
            (2, "", "grep: Unmatched ( or \\(", False),  # literal -F fallback also fails
            (1, "", "", False),
            (0, "a.py:1:x\na.py:2:x\n", "", True),
        ]
    )
    box = ToolBox(ws)
    out = box.call("list_files", {"path": "  ", "max_entries": 2})
    assert out == "a.py\nb.py\n... 1 more entries; narrow the path or raise max_entries"
    assert ws.commands[0].endswith("-- .") and "head" not in ws.commands[0]

    assert box.call("search", {"pattern": "("}).startswith("error: grep: Unmatched")
    assert box.call("search", {"pattern": "zzz"}).startswith("no matches. Do not guess names")
    out = box.call("search", {"pattern": "x", "max_results": 1})
    assert out.startswith("a.py:1:x\n... 1 more matches") and out.endswith("(the workspace truncated this output)")
    assert "--exclude-dir=node_modules" in ws.commands[-1] and "|" not in ws.commands[-1]


def test_repeated_identical_calls_trigger_a_nudge() -> None:
    same = lambda: reply(None, call("run", command="pytest"))  # noqa: E731
    llm = FakeLLM(
        [
            same(),
            same(),
            same(),
            reply(None, call("finish", summary="stuck")),
            reply(None, call("finish", summary="stuck")),
        ]
    )
    events: list[tuple[str, dict]] = []
    result = Agent(llm, FakeToolBox(), max_steps=10, on_event=lambda k, **p: events.append((k, p))).run("t")
    assert result.finished
    nudges = [m for m in llm.requests[-1][0] if m["role"] == "user" and m["content"] == REPEAT_NUDGE]
    assert len(nudges) == 1 and ("nudge", {"step": 3, "reason": "the same call was repeated three times"}) in events


def test_json_safe_serialises_dataclasses_and_containers() -> None:
    @dataclass
    class Usage:
        prompt_tokens: int
        completion_tokens: int

    out = json_safe({"usage": Usage(1, 2), "items": [Usage(3, 4), "x", Path("/p")]})
    assert json.loads(json.dumps(out)) == {
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        "items": [{"prompt_tokens": 3, "completion_tokens": 4}, "x", "/p"],
    }


def test_suspicious_files_match_on_basename() -> None:
    names = ["src/app.py", ".env", "config/.env.production", "deploy/id_rsa", "certs/server.pem", "docs/env.md"]
    assert suspicious_files(names) == [".env", "config/.env.production", "deploy/id_rsa", "certs/server.pem"]
