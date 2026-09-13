"""The system prompt: shipped as a file, printable, replaceable per user, never set by a repository."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from prax_bot.core.agent import DEFAULT_SYSTEM_PROMPT_PATH, SYSTEM_PROMPT, Agent
from prax_bot.core.tools import ToolBox
from prax_bot.settings import Settings
from prax_bot.ui.cli import app
from prax_bot.workspace.sandbox import SandboxWorkspace
from tests.fakes import FakeLLM, FakeToolBox, base, call, fake_client, reply
from tests.helpers import make_session


def test_the_built_in_prompt_is_the_packaged_file_and_says_the_important_things() -> None:
    assert Path(str(DEFAULT_SYSTEM_PROMPT_PATH)).read_text(encoding="utf-8").strip() == SYSTEM_PROMPT
    for phrase in (
        "For bug reports",
        "only with baseline evidence",
        "only when an explicit requirement contradicts it",
        "not a way to start over",
        "Read-only tasks may need no edits",
        "fixed number of steps",
        "Respond only with tool calls",
    ):
        assert phrase in SYSTEM_PROMPT, phrase
    assert len(SYSTEM_PROMPT) < 3700, "keep the core instructions concise"


def test_agent_uses_the_built_in_prompt_unless_given_another() -> None:
    llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
    Agent(llm, FakeToolBox(), max_steps=3).run("t")
    system = llm.requests[0][0][0]
    assert system["role"] == "system" and system["content"].startswith(SYSTEM_PROMPT)
    assert "Code style:" in system["content"], "the code-style prompt is appended"
    llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
    Agent(llm, FakeToolBox(), max_steps=3, system_prompt="You are a terse bot.").run("t")
    assert llm.requests[0][0][0]["content"].startswith("You are a terse bot.\n\nCode style:")


def test_disabling_code_style_keeps_runtime_context_with_a_custom_core_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prax_bot.core import prompts

    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "code_style.md").write_text("\n")  # an empty override switches it off
    monkeypatch.setenv("PRAXBOT_HOME", str(home))
    prompts.clear_cache()
    try:
        llm = FakeLLM([reply(None, call("finish", summary="x")), reply(None, call("finish", summary="y"))])
        Agent(llm, FakeToolBox(), max_steps=3, system_prompt="Terse.").run("t")
        system = llm.requests[0][0][0]["content"]
        assert system.startswith("Terse.\n\nExecution environment:")
        assert "Code style:" not in system and SYSTEM_PROMPT not in system
        assert "remote Linux sandbox" in system
        assert "No commands are scheduled by the runtime" in system
    finally:
        prompts.clear_cache()


@pytest.mark.parametrize("workdir", ["/work", "/testbed"])
def test_sandbox_prompt_describes_the_actual_command_directory(workdir: str) -> None:
    log: list = []
    ws = SandboxWorkspace(fake_client(log), base(log), workdir=workdir)
    system = Agent(FakeLLM([]), ToolBox(ws)).system_message()
    ws.run("pwd", mutate=False)
    assert "remote Linux sandbox" in system
    assert f"initial command directory are {log[-1]['cwd']}" in system
    assert log[-1]["cwd"] == workdir
    assert "{workdir}" not in system and "{scratch_dir}" not in system
    assert "virtual repository root" not in system


@pytest.mark.parametrize("filename", ["evidence.txt", "nested/a b;$(touch marker).txt"])
def test_local_prompt_describes_host_execution_and_usable_scratch_paths(tmp_path: Path, filename: str) -> None:
    session = make_session(tmp_path)
    try:
        ws = session.ws
        box = ToolBox(ws)
        system = Agent(FakeLLM([]), box).system_message()
        assert "user's machine" in system and "without isolation" in system
        assert f"{ws.workdir} as a virtual repository root" in system
        assert "remote Linux sandbox" not in system
        assert '"$TMPDIR/praxbot/scratch"' in system
        output = box.call("write_file", {"path": f"/tmp/praxbot/scratch/{filename}", "content": "evidence\n"}).output
        shell_path = output.split("Local shell path: ", 1)[1].splitlines()[0]
        result = ws.run(f"cat {shell_path}")
        assert result.exit_code == 0 and result.stdout == "evidence\n"
        assert ws.diff() == "", "scratch files stay outside the repository"
    finally:
        session.close()


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
    assert out.exit_code == 0 and out.output.strip() == SYSTEM_PROMPT
    out = CliRunner().invoke(app, ["prompt", "--path"], env=env)
    assert out.exit_code == 0 and "built-in" in out.output and "system.md" in out.output
    custom = tmp_path / "mine.md"
    custom.write_text("Be brief.")
    out = CliRunner().invoke(app, ["prompt"], env={**env, "PRAXBOT_SYSTEM_PROMPT_FILE": str(custom)})
    assert out.exit_code == 0 and out.output.strip() == "Be brief."


def test_prompt_loader_lists_renders_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prax_bot.core import prompts

    names = prompts.names()
    assert {"system", "tools/run", "continuation", "environment/local", "environment/contree"} <= set(names)
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


def test_prompt_full_renders_the_assembled_system_message_for_a_backend(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from prax_bot.ui.cli import app

    monkeypatch.setenv("PRAXBOT_HOME", str(tmp_path))
    monkeypatch.setenv("PRAXBOT_TEST_COMMAND", "pytest -q")
    out = CliRunner().invoke(app, ["prompt", "--full", "--backend", "local"])
    assert out.exit_code == 0, out.output
    assert out.output.startswith("You are Prax Bot")
    assert "Code style:" in out.output and "Execution environment:" in out.output
    assert "on the user's machine" in out.output, "the local backend's environment section"
    assert "Verification plan:" in out.output and "- tests: pytest -q" in out.output
    plain = CliRunner().invoke(app, ["prompt", "--full"])
    assert "remote Linux sandbox" in plain.output and "No commands are scheduled" not in plain.output
