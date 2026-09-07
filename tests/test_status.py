"""praxbot status: what is running, what happened recently, and whether the tool is set up."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from prax_bot.cli import app
from prax_bot.runs import ago, run_started, status_summary
from tests.test_runs import make_run, settings


def make_in_progress(home: Path, run_id: str, *, age_s: int = 0) -> Path:
    d = home / "runs" / run_id
    d.mkdir(parents=True)
    ev = d / "events.jsonl"
    ev.write_text(json.dumps({"kind": "tool.call", "step": 7, "name": "run", "args": "{}"}) + "\n")
    if age_s:
        stamp = time.time() - age_s
        os.utime(ev, (stamp, stamp))
    return d


def test_status_summary_classifies_runs(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    d = make_run(tmp_path, "20260902T000000Z-bbb")
    info = json.loads((d / "run.json").read_text())
    info.update({"goal": {"objective": "x", "rounds": 2, "met": True}, "applied": True})
    (d / "run.json").write_text(json.dumps(info))
    d = make_run(tmp_path, "20260903T000000Z-ccc")
    info = json.loads((d / "run.json").read_text())
    info.update({"resumed_from": {"run_id": "20260901T000000Z-aaa", "step": 1}, "finished": False, "reason": "budget"})
    (d / "run.json").write_text(json.dumps(info))
    make_in_progress(tmp_path, "20260904T000000Z-ddd")
    make_in_progress(tmp_path, "20260904T000100Z-eee", age_s=3 * 3600)

    summary = status_summary(s, limit=10)
    by_id = {r.run_id: r for r in summary["runs"]}
    assert summary["total_runs"] == 5 and [r.run_id for r in summary["runs"]][0] == "20260904T000100Z-eee"
    assert by_id["20260901T000000Z-aaa"].kind == "run" and by_id["20260901T000000Z-aaa"].state == "finished"
    assert by_id["20260901T000000Z-aaa"].tokens == 120 and by_id["20260901T000000Z-aaa"].files_changed == 1
    assert by_id["20260902T000000Z-bbb"].kind == "goal" and by_id["20260902T000000Z-bbb"].applied is True
    assert by_id["20260903T000000Z-ccc"].kind == "resume" and by_id["20260903T000000Z-ccc"].state == "stopped: budget"
    assert by_id["20260904T000000Z-ddd"].state == "in progress" and by_id["20260904T000000Z-ddd"].last_tool == "run (step 7)"
    assert by_id["20260904T000100Z-eee"].state == "abandoned"
    assert run_started("20260904T000000Z-ddd") == datetime(2026, 9, 4, tzinfo=timezone.utc)


def test_ago_formats_durations() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert ago(datetime(2026, 9, 7, 11, 59, 30, tzinfo=timezone.utc), now) == "30s ago"
    assert ago(datetime(2026, 9, 7, 11, 30, tzinfo=timezone.utc), now) == "30m ago"
    assert ago(datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc), now) == "3h ago"
    assert ago(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc), now) == "6d ago" and ago(None) == "?"


def test_status_command_renders(tmp_path: Path) -> None:
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_in_progress(tmp_path, "20260904T000000Z-ddd")
    env = {"PRAXBOT_HOME": str(tmp_path), "NEBIUS_API_KEY": "k", "NEBIUS_PROJECT_ID": "p", "COLUMNS": "200"}
    result = CliRunner().invoke(app, ["status"], env=env)
    assert result.exit_code == 0, result.output
    assert "in progress" in result.output and "20260904T000000Z-ddd" in result.output and "run (step 7)" in result.output
    assert "fix <it>" in result.output and "finished" in result.output and "2 under" in result.output
    empty = CliRunner().invoke(app, ["status"], env={**env, "PRAXBOT_HOME": str(tmp_path / "none")})
    assert empty.exit_code == 0 and "no runs yet" in empty.output
