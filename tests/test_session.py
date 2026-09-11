"""The interactive session: instructions on one workspace, /diff, /undo, /apply, /status, /effort, /quit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from prax_bot.runtime.runs import load_run, new_run, reopen_workspace
from prax_bot.runtime.session import Session
from prax_bot.ui.session import session_loop
from tests.fakes import FakeLLM, call, reply
from tests.helpers import git_repo, make_run, make_snapshot, settings


def test_session_runs_instructions_undoes_applies_and_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, None, "20260902T000000Z-sess")
    root = git_repo(tmp_path / "tree")
    llm = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="1\n")),
            reply(None, call("finish", summary="wrote b")),
            reply(None, call("write_file", path="c.py", content="2\n")),
            reply(None, call("finish", summary="wrote c")),
            reply(None, call("write_file", path="d.py", content="3\n")),
            reply(None, call("finish", summary="wrote d")),
        ]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)
    lines = iter(
        [
            "add b",
            "/status",
            "/diff",
            "add c",
            "/undo",
            "/effort low",
            "/effort medium",
            "/apply",
            "add d",
            "/bogus",
            "/quit",
        ]
    )
    run_id, run_dir, events_file, on_event = new_run(s)
    session = Session(
        s,
        ws,
        root,
        backend="local",
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        protect=[],
    )
    session_loop(session, Console(), lambda: next(lines, None))

    assert (root / "b.py").read_text() == "1\n" and not (root / "c.py").exists(), (
        "/undo dropped c before /apply, so only b was applied"
    )
    assert session.settings.effort == "low", "a bad level is rejected and the previous one kept"
    assert (root / "d.py").exists() is False, "work after /apply is pending until applied"
    assert "d.py" in ws.diff() and "b.py" not in ws.diff(), "after /apply the sandbox baseline moved forward"
    info = json.loads((run_dir / "run.json").read_text())
    assert (
        info["task"] == "add b"
        and info["followups"] == ["add d"]
        and info["session"] == {"rounds": 2, "applied_rounds": 1}
    )
    assert info["applied"] is True and info["steps"] == 6
    second_round_task = next(
        m["content"]
        for m in llm.requests[2][0]
        if m["role"] == "user" and m["content"].startswith("You are continuing")
    )
    assert "Original task:\nadd b" in second_round_task and second_round_task.endswith("Now do this:\nadd c")
    kinds = [json.loads(line)["kind"] for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert kinds.count("instruction") == 3


def test_session_goal_command_parses_check_and_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, None, "20260902T000000Z-goal")
    llm = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="1\n")),
            reply(None, call("finish", summary="not yet", goal_met=False)),
            reply(None, call("write_file", path="c.py", content="2\n")),
            reply(None, call("finish", summary="done", goal_met=True)),
        ]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)
    lines = iter(['/goal --rounds 3 "b and c exist"', "/quit"])
    run_id, run_dir, events_file, on_event = new_run(s)
    session = Session(
        s,
        ws,
        tmp_path,
        backend="local",
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        protect=[],
    )
    session_loop(session, Console(), lambda: next(lines, None))
    info = json.loads((run_dir / "run.json").read_text())
    assert info["session"]["rounds"] == 2 and info["task"] == "b and c exist" and info["summary"] == "done"
    assert "b.py" in ws.diff() and "c.py" in ws.diff()
