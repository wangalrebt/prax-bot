"""The system prompt: shipped as a file, printable, replaceable per user, never set by a repository."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from prax_bot.core.agent import DEFAULT_SYSTEM_PROMPT_PATH, SYSTEM_PROMPT, Agent
from prax_bot.settings import Settings
from prax_bot.ui.cli import app
from tests.fakes import FakeLLM, FakeToolBox, call, reply


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
    system = llm.requests[0][0][0]
    assert system["role"] == "system" and system["content"].startswith(SYSTEM_PROMPT)
    assert "Code style:" in system["content"], "the code-style prompt is appended"
    llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
    Agent(llm, FakeToolBox(), max_steps=3, system_prompt="You are a terse bot.").run("t")
    assert llm.requests[0][0][0]["content"].startswith("You are a terse bot.\n\nCode style:")


def test_code_style_prompt_says_the_important_things_and_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prax_bot.core import prompts

    style = prompts.prompt("code_style")
    for phrase in (
        "Match the surrounding code",
        "No drive-by",
        "Comments explain why",
        "run it on the files you changed",
        "add, do not break",
    ):
        assert phrase in style, phrase
    assert len(style) < 1800
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "code_style.md").write_text("\n")  # an empty override switches it off
    monkeypatch.setenv("PRAXBOT_HOME", str(home))
    prompts.clear_cache()
    try:
        llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
        Agent(llm, FakeToolBox(), max_steps=3, system_prompt="Terse.").run("t")
        assert llm.requests[0][0][0]["content"] == "Terse."
    finally:
        prompts.clear_cache()


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
    assert out.exit_code == 0 and "built-in" in out.output and "system.md" in out.output
    custom = tmp_path / "mine.md"
    custom.write_text("Be brief.")
    out = CliRunner().invoke(app, ["prompt"], env={**env, "PRAXBOT_SYSTEM_PROMPT_FILE": str(custom)})
    assert out.exit_code == 0 and out.output.strip() == "Be brief."


def test_prompt_loader_lists_renders_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prax_bot.core import prompts

    names = prompts.names()
    assert "system" in names and "tools/run" in names and "continuation" in names and len(names) >= 25
    assert prompts.source("system")[0] == "built-in"
    # placeholders: filled when given, left alone when not, and a value is never re-interpreted
    assert prompts.prompt("skill_loaded", name="x", source="s", text="{name} stays") == "Skill x (s):\n{name} stays"
    assert prompts.prompt("late_no_edit").count("{left}") == 1
    assert prompts.prompt("mid_no_edit").count("{left}") == 1
    with pytest.raises(KeyError, match="no prompt named 'nope'"):
        prompts.source("nope")
    # a file of the same name under ~/.praxbot/prompts replaces the packaged text
    home = tmp_path / "home"
    (home / "prompts" / "tools").mkdir(parents=True)
    (home / "prompts" / "nudge.md").write_text("Carry on.\n")
    (home / "prompts" / "tools" / "run.md").write_text("Run a command in {scratch_dir}.\n")
    monkeypatch.setenv("PRAXBOT_HOME", str(home))
    prompts.clear_cache()
    try:
        assert prompts.source("nudge") == ("override", home / "prompts" / "nudge.md")
        assert prompts.prompt("nudge") == "Carry on."
        assert prompts.prompt("tools/run", scratch_dir="/tmp/s") == "Run a command in /tmp/s."
        assert prompts.source("system")[0] == "built-in", "only the files present are overridden"
    finally:
        prompts.clear_cache()


def test_prompt_command_lists_and_prints_any_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"PRAXBOT_HOME": str(home), "NEBIUS_API_KEY": "k", "NEBIUS_PROJECT_ID": "p", "COLUMNS": "200"}
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(app, ["prompt", "--list"], env=env)
    assert (
        out.exit_code == 0
        and "tools/run" in out.output
        and "built-in" in out.output
        and "override any of them" in out.output
    )
    out = CliRunner().invoke(app, ["prompt", "tools/restore"], env=env)
    assert out.exit_code == 0 and "At most three restores" in out.output
    out = CliRunner().invoke(app, ["prompt", "nope"], env=env)
    assert out.exit_code == 1 and "no prompt named" in out.output
