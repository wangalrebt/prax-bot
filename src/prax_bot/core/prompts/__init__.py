"""Every word the model sees, as text files: one per prompt, `{placeholders}` filled in by the code that uses it.

The packaged files are the defaults. A file with the same relative name under ~/.praxbot/prompts/ (PRAXBOT_HOME
respected) replaces it, which is how a user changes what the model is told without touching code. `prax prompt
--list` shows every prompt and where each one comes from.
"""

from __future__ import annotations

import os
import re
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

PACKAGED = resources.files(__name__)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def override_dir() -> Path:
    return Path(os.environ.get("PRAXBOT_HOME") or Path.home() / ".praxbot") / "prompts"


def names() -> list[str]:
    """Every packaged prompt, as the name to pass to prompt(): e.g. "system", "tools/run"."""
    found: list[str] = []
    for entry in PACKAGED.iterdir():
        if entry.name.endswith(".md"):
            found.append(entry.name[:-3])
        elif entry.is_dir() and not entry.name.startswith("_"):
            found.extend(f"{entry.name}/{sub.name[:-3]}" for sub in entry.iterdir() if sub.name.endswith(".md"))
    return sorted(found)


def source(name: str) -> tuple[str, Path]:
    """Where a prompt's text comes from: ("override", path) or ("built-in", path)."""
    custom = override_dir() / f"{name}.md"
    if custom.is_file():
        return "override", custom
    packaged = PACKAGED / f"{name}.md"
    if not packaged.is_file():
        raise KeyError(f"no prompt named {name!r}; prax prompt --list shows them")
    return "built-in", Path(str(packaged))


@cache
def text(name: str) -> str:
    """The raw text of a prompt, placeholders intact."""
    return source(name)[1].read_text(encoding="utf-8").strip()


def prompt(key: str, /, **values: Any) -> str:
    """A prompt with the given placeholders filled in; placeholders not given are left as they are.

    Values are inserted verbatim in one pass, so a diff or a summary containing braces is never re-interpreted.
    """
    raw = text(key)
    if not values:
        return raw
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), raw)
