from __future__ import annotations

from pathlib import Path

import pytest

from prax_bot.runtime.runs import checkpoint_diff, list_runs, load_run
from prax_bot.ui.report import render_html
from tests.helpers import make_run, settings


def test_list_and_load_runs(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_run(tmp_path, "20260902T000000Z-bbb")
    runs = list_runs(s)
    assert [r.run_id for r in runs] == ["20260902T000000Z-bbb", "20260901T000000Z-aaa"]
    run = load_run("20260901T000000Z-aaa", s)
    calls = run.tool_calls
    assert [c["name"] for c in calls] == ["read_file", "edit_file"] and calls[1]["error"] is True
    assert run.diff.startswith("diff --git") and len(run.checkpoints) == 2
    with pytest.raises(FileNotFoundError):
        load_run("nope", s)


def test_render_html_escapes_and_includes_sections(tmp_path: Path) -> None:
    make_run(tmp_path, "20260901T000000Z-aaa")
    page = render_html(load_run("20260901T000000Z-aaa", settings(tmp_path)))
    assert "fix &lt;it&gt;" in page and "done &amp; dusted" in page
    assert "<h2>Steps</h2>" in page and "read_file" in page and "class='err'" in page
    assert "local:snap-1" in page and "<pre>diff --git" in page and "Usage by model" in page


def test_checkpoint_diff_uses_local_snapshots(tmp_path: Path) -> None:
    import subprocess

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    snap = tmp_path / "local" / "20260901T000000Z-aaa" / "snapshots" / "snap-1"
    snap.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=snap, check=True)
    (snap / "a.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=snap, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"], cwd=snap, check=True)
    (snap / "a.py").write_text("y\n")
    run = load_run("20260901T000000Z-aaa", s)
    assert "+y" in checkpoint_diff(run, 1, s)
    with pytest.raises(ValueError):
        checkpoint_diff(run, 5, s)
    with pytest.raises(FileNotFoundError):
        checkpoint_diff(run, 0, s)
