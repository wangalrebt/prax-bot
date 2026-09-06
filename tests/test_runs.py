from __future__ import annotations

import json
from pathlib import Path

import pytest

from prax_bot.runs import checkpoint_diff, list_runs, load_run, render_html
from prax_bot.settings import Settings


def make_run(home: Path, run_id: str, *, backend: str = "local") -> Path:
    d = home / "runs" / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": run_id, "task": "fix <it>", "repo": "/r", "model": "m", "backend": backend, "finished": True, "reason": "finish",
        "steps": 2, "summary": "done & dusted", "model_calls": 2, "prompt_tokens": 100, "completion_tokens": 20,
        "sandbox_commands": 1, "sandbox_cost": 0.0, "baseline_image": "local:snap-0", "final_image": "local:snap-1",
        "checkpoints": [{"step": 0, "image": "local:snap-0", "label": "baseline"}, {"step": 1, "image": "local:snap-1", "label": "write: a.py"}],
        "elapsed_s": 3.2, "usage_by_model": {"m": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 20}},
    }))
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "tool.call", "step": 1, "name": "read_file", "args": json.dumps({"path": "a.py"})},
        {"kind": "tool.result", "step": 1, "name": "read_file", "output": "a.py lines 1-2 of 2\n1| x"},
        {"kind": "tool.call", "step": 2, "name": "edit_file", "args": json.dumps({"path": "a.py", "old": "x", "new": "y"})},
        {"kind": "tool.result", "step": 2, "name": "edit_file", "output": "error: old text was not found"},
    ]) + "\n")
    (d / "diff.patch").write_text("diff --git a/a.py b/a.py\n-x\n+y\n")
    return d


def settings(home: Path) -> Settings:
    return Settings(api_key="k", project_id="p", model="m", llm_base_url="u", base_image="i", setup=None,
                    max_steps=5, command_timeout=5, setup_timeout=5, import_timeout=5, home=home)


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
