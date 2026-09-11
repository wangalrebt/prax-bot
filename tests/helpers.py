"""Temporary Git repositories, run records, and settings used by integration tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from prax_bot.settings import Settings


def make_run(home: Path, run_id: str, *, backend: str = "local") -> Path:
    d = home / "runs" / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "task": "fix <it>",
                "repo": "/r",
                "model": "m",
                "backend": backend,
                "finished": True,
                "reason": "finish",
                "steps": 2,
                "summary": "done & dusted",
                "model_calls": 2,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "sandbox_commands": 1,
                "sandbox_cost": 0.0,
                "baseline_image": "local:snap-0",
                "final_image": "local:snap-1",
                "checkpoints": [
                    {"step": 0, "image": "local:snap-0", "label": "baseline"},
                    {"step": 1, "image": "local:snap-1", "label": "write: a.py"},
                ],
                "elapsed_s": 3.2,
                "usage_by_model": {"m": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 20}},
            }
        )
    )
    (d / "events.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"kind": "tool.call", "step": 1, "name": "read_file", "args": json.dumps({"path": "a.py"})},
                {"kind": "tool.result", "step": 1, "name": "read_file", "output": "a.py lines 1-2 of 2\n1| x"},
                {
                    "kind": "tool.call",
                    "step": 2,
                    "name": "edit_file",
                    "args": json.dumps({"path": "a.py", "old": "x", "new": "y"}),
                },
                {"kind": "tool.result", "step": 2, "name": "edit_file", "output": "error: old text was not found"},
            ]
        )
        + "\n"
    )
    (d / "diff.patch").write_text("diff --git a/a.py b/a.py\n-x\n+y\n")
    return d


def settings(home: Path) -> Settings:
    return Settings(
        api_key="k",
        project_id="p",
        model="m",
        llm_base_url="u",
        base_image="i",
        setup=None,
        max_steps=5,
        command_timeout=5,
        setup_timeout=5,
        import_timeout=5,
        home=home,
    )


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


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"], cwd=path, check=True
    )
    return path


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_sandbox_repo(root: Path) -> Path:
    git("init", "-q", cwd=root)
    (root / "a.py").write_text("print(1)\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("no")
    (root / "sub").mkdir()
    (root / "sub" / "new.txt").write_text("untracked")
    git("add", "a.py", ".gitignore", cwd=root)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=root)
    return root


def sandbox_settings(tmp_path: Path, setup: str | None = None) -> Settings:
    return Settings(
        api_key="k",
        project_id="p",
        model="m",
        llm_base_url="u",
        base_image="python:3.12",
        setup=setup,
        max_steps=5,
        command_timeout=10,
        setup_timeout=20,
        import_timeout=30,
        home=tmp_path,
    )


def make_local_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.py").write_text("print(1)\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def make_session(tmp_path: Path, *, llm=None, **overrides):
    """A real local workspace and run log driven by a scripted model."""
    import dataclasses

    from prax_bot.runtime.runs import new_run
    from prax_bot.runtime.session import Session
    from prax_bot.workspace.local import LocalWorkspace, clone_dir
    from tests.fakes import FakeLLM

    config = dataclasses.replace(settings(tmp_path), backend="local", **overrides)
    repo = git_repo(tmp_path / "repo")
    workspace_root = tmp_path / "local" / "workspace"
    workspace_root.mkdir(parents=True)
    clone_dir(repo, workspace_root / "current")
    workspace = LocalWorkspace(workspace_root, command_timeout=config.command_timeout)
    run_id, run_dir, events_file, on_event = new_run(config)
    return Session(
        config,
        workspace,
        repo,
        backend="local",
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        llm=llm or FakeLLM([]),
    )
