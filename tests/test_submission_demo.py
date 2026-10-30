"""The printed demo command resolves the current model without opening a server or calling an API."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import submission_demo
from typer.testing import CliRunner

from prax_bot.settings import DEFAULT_MODEL
from prax_bot.ui.cli import app
from prax_bot.ui.commands import execution


@pytest.mark.parametrize("model", [None, "custom/model"])
def test_generated_demo_uses_default_or_environment_model_with_a_clean_failing_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], model: str | None
) -> None:
    for key in os.environ:
        if key.startswith("PRAXBOT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path / "auth"))
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "fake-project")
    # A space in the path exercises the shell quoting in the actual printed command.
    directory = tmp_path / "demo directory"
    directory.mkdir()
    monkeypatch.setattr(submission_demo.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    submission_demo.main()
    command = shlex.split(next(line for line in capsys.readouterr().out.splitlines() if line.startswith("env ")))
    env = dict([command[1].split("=", 1)])
    if model is not None:
        env["PRAXBOT_MODEL"] = model
    started = []
    monkeypatch.setattr(execution, "_start_web", lambda *args: started.append(args))

    result = CliRunner().invoke(app, command[command.index("prax") + 1 :], env=env)

    assert result.exit_code == 0, result.output
    assert len(started) == 1
    settings, root, backend, protected, *_ = started[0]
    assert settings.model == (model or DEFAULT_MODEL)
    assert settings.sources["model"] == ("env" if model else "default")
    assert settings.home == directory / "home"
    assert (backend, settings.effort, settings.max_steps) == ("contree", "low", 12)
    assert protected == ["test_calc.py"]
    assert settings.test_command == "python -B -m unittest -v"
    assert root == (directory / "repo").resolve()
    baseline = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-v"], cwd=root, capture_output=True, text=True, check=False
    )
    assert baseline.returncode == 1 and "AssertionError: -1 != 5" in baseline.stderr
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True) == ""
