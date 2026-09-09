from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from prax_bot.settings import Settings
from prax_bot.workspace.local import LocalWorkspace, create_local_workspace


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.py").write_text("print(1)\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def workspace(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "ws"
    (root / "current").mkdir(parents=True)
    make_repo(root / "current")
    return LocalWorkspace(root, command_timeout=10)


def test_run_read_write_restore_and_diff(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    assert ws.image_id == "local:snap-0" and ws.checkpoints[0].label == "baseline"

    r = ws.run("echo hi && touch marker")
    assert (r.exit_code, r.stdout, r.image) == (0, "hi\n", "local:snap-1")
    assert ws.read("marker") == b""

    ro = ws.run("touch not-kept", mutate=False)
    assert ro.exit_code == 0 and ro.image is None
    with pytest.raises(FileNotFoundError):
        ws.read("not-kept")

    ws.write("pkg/new.py", b"x = 1\n")
    assert ws.read("pkg/new.py") == b"x = 1\n" and ws.current_step == 2

    diff = ws.diff()
    assert "pkg/new.py" in diff and "marker" in diff and ws.current_step == 2

    ws.restore(0)
    assert ws.current_step == 3 and ws.checkpoints[-1].label == "restore: step 0"
    with pytest.raises(FileNotFoundError):
        ws.read("marker")
    assert ws.diff() == ""


def test_timeout_kills_the_command(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    r = ws.run("sleep 5", timeout=1)
    assert r.timed_out and r.exit_code == 124 and r.elapsed_s < 4


def test_environment_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "secret")
    ws = workspace(tmp_path)
    r = ws.run('echo "key=${NEBIUS_API_KEY:-unset} home=$HOME"', mutate=False)
    assert r.stdout.strip() == f"key=unset home={ws.root / 'home'}"


def test_create_local_workspace_builds_baseline_and_venv(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    settings = Settings(
        api_key="k",
        project_id=None,
        model="m",
        llm_base_url="u",
        base_image="python:3.12",
        setup="python -c 'import sys; print(sys.version_info[:2])'",
        max_steps=5,
        command_timeout=30,
        setup_timeout=120,
        import_timeout=30,
        home=tmp_path / "home",
    )
    events: list[str] = []
    ws = create_local_workspace(settings, repo, lambda k, **p: events.append(k), run_id="t1")
    assert events[:4] == ["sandbox.pack", "sandbox.base", "checkpoint", "sandbox.ready"] and "sandbox.setup" in events
    assert (ws.venv / "bin" / "python").exists()
    assert (
        ws.run(
            "python -c 'import sys; print(sys.version_info.major, sys.version_info.minor)'", mutate=False
        ).stdout.strip()
        == "3 12"
    )
    assert ws.diff() == ""
    ws.run("mkdir -p pkg.egg-info __pycache__ && touch pkg.egg-info/PKG-INFO __pycache__/x.pyc real.txt")
    diff = ws.diff()
    assert "real.txt" in diff and "egg-info" not in diff and "pyc" not in diff
