"""AGENTS.md in subdirectories, skills loaded on demand, and plugins that add tools."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from prax_bot.agent import Agent
from prax_bot.plugins import load_plugins
from prax_bot.skills import Skill, home_skills, overview, parse_skill, repo_skills
from prax_bot.tools import ToolBox
from tests.test_agent import FakeLLM, call, reply
from tests.test_tools import FakeWorkspace

ROOT = Path(__file__).resolve().parents[1]


def test_parse_skill_front_matter_and_fallbacks() -> None:
    name, desc, body = parse_skill("---\nname: deploy\ndescription: 'Ship it safely'\n---\n# Deploy\nsteps", "dir")
    assert (name, desc, body) == ("deploy", "Ship it safely", "# Deploy\nsteps")
    name, desc, body = parse_skill("# Just a heading\ntext", "fallback")
    assert name == "fallback" and desc == "Just a heading" and body.startswith("# Just")


def test_home_and_repo_skills_are_discovered(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "examples" / "skills", tmp_path / "skills")
    (tmp_path / "skills" / "bad name!").mkdir()
    (tmp_path / "skills" / "bad name!" / "SKILL.md").write_text("---\nname: Bad Name!\n---\nx")
    personal = home_skills(tmp_path)
    assert [s.name for s in personal] == ["pytest-failures"] and personal[0].body.startswith("# Reading")
    ws = FakeWorkspace(
        {
            ".praxbot/skills/release/SKILL.md": "---\nname: release\ndescription: Cut a release\n---\nrun make release\n",
            "a.py": "x\n",
        }
    )
    found = repo_skills(ws, [".praxbot/skills/release/SKILL.md", "a.py", ".praxbot/skills/nested/too/deep/SKILL.md"])
    assert [s.name for s in found] == ["release"] and found[0].text() == "run make release"
    assert overview(found).startswith("Skills available") and "- release: Cut a release" in overview(found)
    assert overview([]) == ""


def test_toolbox_advertises_skills_and_loads_them_on_demand() -> None:
    ws = FakeWorkspace(
        {
            ".praxbot/skills/release/SKILL.md": "---\nname: release\ndescription: Cut a release\n---\nrun make release\n",
            "a.py": "x\n",
        }
    )
    plain = ToolBox(FakeWorkspace({"a.py": "x\n"}))
    assert "load_skill" not in {s["function"]["name"] for s in plain.specs()} and plain.skills_overview() == ""
    box = ToolBox(ws, skills=[Skill("deploy", "Ship it", "~/.praxbot/skills/deploy", body="1. tag\n2. push")])
    box._paths = list(ws.files)
    text = box.skills_overview()
    assert "- deploy: Ship it" in text and "- release: Cut a release" in text
    names = {s["function"]["name"] for s in box.specs()}
    assert "load_skill" in names and "finish" in names
    assert (
        box.call("load_skill", json.dumps({"name": "deploy"}))
        == "Skill deploy (~/.praxbot/skills/deploy):\n1. tag\n2. push"
    )
    assert box.call("load_skill", json.dumps({"name": "release"})).endswith("run make release")
    assert box.call("load_skill", json.dumps({"name": "nope"})).startswith(
        "error: no skill named 'nope'. Available: deploy, release"
    )


def test_nested_instructions_are_shown_once_per_directory() -> None:
    ws = FakeWorkspace(
        {
            "pkg/AGENTS.md": "Use tabs here.\n",
            "pkg/a.py": "x\n",
            "pkg/b.py": "y\n",
            "AGENTS.md": "root rules\n",
            "top.py": "z\n",
        }
    )
    box = ToolBox(ws)
    first = box.call("read_file", json.dumps({"path": "pkg/a.py"}))
    assert "Instructions from pkg/AGENTS.md apply here:\nUse tabs here." in first
    second = box.call("read_file", json.dumps({"path": "pkg/b.py"}))
    assert "Instructions from" not in second, "shown once per directory"
    assert "Instructions from" not in box.call("read_file", json.dumps({"path": "top.py"})), (
        "the root file is handled at the start, not here"
    )


def test_plugins_load_from_home_and_broken_ones_are_skipped(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    shutil.copy(ROOT / "examples" / "plugins" / "lint.py", plugins / "lint.py")
    (plugins / "broken.py").write_text("def register(add):\n    add('read_file', 'clash', None, None)\n")
    (plugins / "syntax.py").write_text("this is not python\n")
    warnings: list[str] = []
    tools = load_plugins(tmp_path, {"read_file"}, warnings.append)
    assert [t.name for t in tools] == ["lint"] and tools[0].spec()["function"]["name"] == "lint"
    assert (
        len(warnings) == 2
        and any("broken.py" in w and "already taken" in w for w in warnings)
        and any("syntax.py" in w for w in warnings)
    )

    class WS(FakeWorkspace):
        def run(self, command, *, timeout=None, mutate=True, truncate_output_at=None):
            assert mutate is False and "ruff check" in command
            return SimpleNamespace(exit_code=1, stdout="a.py:1:8: F401 `os` imported but unused\n", stderr="")

    box = ToolBox(WS({"a.py": "import os\n"}), plugins=tools)
    assert "lint" in {s["function"]["name"] for s in box.specs()}
    out = box.call("lint", json.dumps({"path": "a.py"}))
    assert out.startswith("ruff check a.py: exit 1") and "F401" in out
    assert box.call("nope", "{}").startswith("error: unknown tool 'nope'") and "lint" in box.call("nope", "{}")


def test_agent_uses_toolbox_specs_and_advertises_skills() -> None:
    ws = FakeWorkspace(
        {
            ".praxbot/skills/release/SKILL.md": "---\nname: release\ndescription: Cut a release\n---\nbody\n",
            "a.py": "x\n",
        }
    )
    box = ToolBox(ws)
    box._paths = list(ws.files)
    load = SimpleNamespace(
        id="call-load_skill", function=SimpleNamespace(name="load_skill", arguments=json.dumps({"name": "release"}))
    )
    # the first finish without an edit is refused once, so the model finishes twice
    llm = FakeLLM(
        [
            reply(None, load),
            reply(None, call("finish", summary="done")),
            reply(None, call("finish", summary="done, nothing to change")),
        ]
    )
    agent = Agent(llm, box, max_steps=5)
    result = agent.run("release it")
    assert result.finished
    messages, _ = llm.requests[0]
    assert any(m["role"] == "user" and m["content"].startswith("Skills available") for m in messages)
    assert "load_skill" in {t["function"]["name"] for t in agent.tools}
    assert any(m["role"] == "tool" and m["content"].startswith("Skill release") for m in llm.requests[-1][0])
