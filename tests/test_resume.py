"""Resuming a run from a checkpoint and continuing with follow-up instructions on the same workspace."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from prax_bot.runs import continuation_task, load_run, reopen_workspace
from tests.test_agent import FakeLLM, call, reply
from tests.test_runs import make_run, settings


def make_snapshot(home: Path, run_id: str, step: int, *, changed: bool = True) -> Path:
    """A local snapshot that looks like the output of a run: a git baseline plus an uncommitted change."""
    snap = home / "local" / run_id / "snapshots" / f"snap-{step}"
    snap.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=snap, check=True)
    (snap / "a.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=snap, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"], cwd=snap, check=True
    )
    if changed:
        (snap / "a.py").write_text("y\n")
    return snap


def test_continuation_task_carries_task_summary_diff_and_instruction() -> None:
    text = continuation_task(
        "fix the parser", "changed parse()", "diff --git a/p.py b/p.py\n-x\n+y\n", "also handle negatives"
    )
    assert text.index("Original task:\nfix the parser") < text.index("Summary of the earlier work:\nchanged parse()")
    assert "```diff\ndiff --git a/p.py b/p.py\n-x\n+y\n```" in text
    assert text.endswith("Now do this:\nalso handle negatives")
    assert "(none)" in continuation_task("t", None, "", "go") and "(no changes yet)" in continuation_task(
        "t", "", "", "go"
    )
    long = continuation_task("t", "s", "+" * 50_000, "go")
    assert "diff truncated" in long and len(long) < 31_000


def test_reopen_workspace_rebuilds_a_local_checkpoint(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    snap = make_snapshot(tmp_path, "20260901T000000Z-aaa", 1)
    run = load_run("20260901T000000Z-aaa", s)

    ws = reopen_workspace(run, 1, s, None, "20260902T000000Z-new")
    assert ws.root == tmp_path / "local" / "20260902T000000Z-new"
    assert "+y" in ws.diff(), "the earlier change is present in the rebuilt workspace"
    assert ws.run("cat a.py", mutate=False).stdout == "y\n"
    ws.write("b.py", b"new\n")
    assert (snap / "b.py").exists() is False, "the old snapshot is not touched"
    assert "b.py" in ws.diff() and len(ws.checkpoints) == 2

    with pytest.raises(ValueError):
        reopen_workspace(run, 5, s, None, "x")
    with pytest.raises(FileNotFoundError):
        reopen_workspace(run, 0, s, None, "x")  # snap-0 was never created


def test_solve_continues_on_the_same_workspace_after_a_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import prax_bot.llm
    from prax_bot import cli

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    run = load_run("20260901T000000Z-aaa", s)
    ws = reopen_workspace(run, 1, s, None, "20260902T000000Z-new")

    llm = FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="1\n")),
            reply(None, call("finish", summary="wrote b")),
            reply(None, call("write_file", path="c.py", content="2\n")),
            reply(None, call("finish", summary="wrote c")),
        ]
    )
    monkeypatch.setattr(prax_bot.llm, "LLM", lambda settings: llm)
    answers = iter(["also add c", ""])
    run_id, run_dir, events_file, on_event = cli._new_run(s, verbose=False)
    cli._solve(
        s,
        ws,
        tmp_path,
        "add b",
        backend="local",
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=False,
        export=None,
        protect=[],
        started=time.monotonic(),
        ask_followup=lambda: next(answers),
    )

    info = json.loads((run_dir / "run.json").read_text())
    assert info["task"] == "add b" and info["followups"] == ["also add c"] and info["steps"] == 4
    assert info["summary"] == "wrote c" and info["finished"] is True
    patch = (run_dir / "diff.patch").read_text()
    assert "b/b.py" in patch and "b/c.py" in patch, "both rounds' work is in the final diff"
    second_round_task = next(
        m["content"]
        for m in llm.requests[2][0]
        if m["role"] == "user" and m["content"].startswith("You are continuing")
    )
    assert "Original task:\nadd b" in second_round_task and "wrote b" in second_round_task
    assert "+1" in second_round_task and second_round_task.endswith("Now do this:\nalso add c")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [e["instruction"] for e in events if e["kind"] == "followup"] == ["also add c"]
