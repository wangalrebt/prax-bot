"""Skills: SKILL.md files the model can load on demand.

A skill is a directory with a SKILL.md whose front matter names and describes it. Only the names and
descriptions are shown at the start of a run; the full text arrives when the model calls load_skill.
Repository skills live under .praxbot/skills/<name>/SKILL.md and are read inside the sandbox.
Personal skills live under ~/.praxbot/skills/<name>/SKILL.md on this machine.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_SKILLS_DIR = ".praxbot/skills"
MAX_SKILLS = 40
MAX_SKILL_CHARS = 20_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass
class Skill:
    name: str
    description: str
    source: str  # where it came from, shown to the user
    body: str | None = None  # personal skills are read up front
    reader: Callable[[], str] | None = None  # repository skills are read from the workspace on demand

    def text(self) -> str:
        text = self.body if self.body is not None else (self.reader() if self.reader else "")
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + "\n… (truncated)"
        return text


def parse_skill(text: str, fallback_name: str) -> tuple[str, str, str]:
    """(name, description, body) from a SKILL.md with YAML-style front matter; tolerant of a missing block."""
    name, description, body = fallback_name, "", text
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip().strip("'\"")
            if key == "name" and value:
                name = value
            elif key == "description" and value:
                description = value
        body = m.group(2)
    if not description:
        first = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
        description = first[:200]
    return name, description, body.strip()


def home_skills(home: Path) -> list[Skill]:
    base = home / "skills"
    if not base.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(base.glob("*/SKILL.md"))[:MAX_SKILLS]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name, description, body = parse_skill(text, path.parent.name)
        if NAME_RE.match(name):
            skills.append(Skill(name, description, f"~/.praxbot/skills/{path.parent.name}", body=body))
    return skills


def repo_skill_paths(paths: list[str]) -> list[str]:
    prefix = REPO_SKILLS_DIR + "/"
    found = [p for p in paths if p.startswith(prefix) and p.endswith("/SKILL.md") and p.count("/") == 3]
    return sorted(found)[:MAX_SKILLS]


def repo_skills(workspace: Any, paths: list[str]) -> list[Skill]:
    """Skills committed in the repository; their front matter is read now, the body when loaded."""
    skills: list[Skill] = []
    for path in repo_skill_paths(paths):
        try:
            text = workspace.read(path).decode("utf-8", "replace")
        except (OSError, ValueError):
            continue
        name, description, body = parse_skill(text, path.split("/")[-2])
        if NAME_RE.match(name):
            skills.append(Skill(name, description, path, body=body))
    return skills


def overview(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return (
        "Skills available. Before doing work that a skill covers, call load_skill with its name and follow it:\n"
        + "\n".join(lines)
    )
