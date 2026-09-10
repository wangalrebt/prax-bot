"""The system prompt: shipped as a file, printable, replaceable per user, never set by a repository."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from prax_bot.core.agent import DEFAULT_SYSTEM_PROMPT_PATH, SYSTEM_PROMPT, Agent
from prax_bot.settings import Settings
from prax_bot.ui.cli import app
from tests.test_agent import FakeLLM, FakeToolBox, call, reply


def test_the_built_in_prompt_is_the_packaged_file_and_says_the_important_things() -> None:
    assert Path(str(DEFAULT_SYSTEM_PROMPT_PATH)).read_text(encoding="utf-8").strip() == SYSTEM_PROMPT
    for phrase in (
        "Reproduce before you change anything",
        "not a way to start over",
        "assert the old behaviour",
        "fixed number of steps",
        "Respond only with tool calls",
    ):
        assert phrase in SYSTEM_PROMPT, phrase
    assert len(SYSTEM_PROMPT) < 3500, "kept short on purpose"


def test_agent_uses_the_built_in_prompt_unless_given_another() -> None:
    llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
    Agent(llm, FakeToolBox(), max_steps=3).run("t")
    assert llm.requests[0][0][0] == {"role": "system", "content": SYSTEM_PROMPT}
    llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
    Agent(llm, FakeToolBox(), max_steps=3, system_prompt="You are a terse bot.").run("t")
    assert llm.requests[0][0][0]["content"] == "You are a terse bot."


def test_system_prompt_file_is_user_level_and_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PRAXBOT_HOME", str(home))
    monkeypatch.delenv("PRAXBOT_SYSTEM_PROMPT_FILE", raising=False)
    assert Settings.from_env().system_prompt_text() is None
    custom = tmp_path / "mine.md"
    custom.write_text("Be brief.\n")
    (home / "config.toml").write_text(f'system_prompt_file = "{custom}"\n')
    s = Settings.from_env()
    assert s.system_prompt_text() == "Be brief." and s.sources["system_prompt_file"] == "user config"
    monkeypatch.setenv("PRAXBOT_SYSTEM_PROMPT_FILE", str(tmp_path / "missing.md"))
    with pytest.raises(ValueError, match="could not be read"):
        Settings.from_env()
    monkeypatch.delenv("PRAXBOT_SYSTEM_PROMPT_FILE")
    (home / "config.toml").write_text("")
    repo = tmp_path / "r"
    (repo / ".praxbot").mkdir(parents=True)
    (repo / ".praxbot" / "config.toml").write_text(f'system_prompt_file = "{custom}"\n')
    s = Settings.from_env(repo_root=repo)
    assert s.system_prompt_text() is None and any(
        "'system_prompt_file' may not be set by a repository" in w for w in s.warnings
    )


def test_prompt_command_prints_the_text_and_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"PRAXBOT_HOME": str(home), "NEBIUS_API_KEY": "k", "NEBIUS_PROJECT_ID": "p"}
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(app, ["prompt"], env=env)
    assert out.exit_code == 0 and "Reproduce before you change anything" in out.output
    out = CliRunner().invoke(app, ["prompt", "--path"], env=env)
    assert out.exit_code == 0 and "built in" in out.output and "system.md" in out.output
    custom = tmp_path / "mine.md"
    custom.write_text("Be brief.")
    out = CliRunner().invoke(app, ["prompt"], env={**env, "PRAXBOT_SYSTEM_PROMPT_FILE": str(custom)})
    assert out.exit_code == 0 and out.output.strip() == "Be brief."
