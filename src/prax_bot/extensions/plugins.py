"""Plugins: Python files under ~/.praxbot/plugins/ that add tools to the agent.

A plugin defines register(add), and calls add(name, description, model, handler) for each tool: model is a
pydantic class for the arguments, handler(workspace, args) returns successful text or a ToolResult with an
explicit error flag. Handlers run on
this machine, not in the sandbox, which is why plugins are loaded only from the home directory and never
from the repository being worked on.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prax_bot.core.tool_result import ToolResult

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass
class ExtraTool:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[Any, Any], str | ToolResult]
    source: str = ""

    def spec(self) -> dict[str, Any]:
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }


def _load_one(path: Path, taken: set[str]) -> list[ExtraTool]:
    """Import one plugin file and collect the tools it registers. Raises on any problem."""
    added: list[ExtraTool] = []

    def add(
        name: str, description: str, model: type[BaseModel], handler: Callable[[Any, Any], str | ToolResult]
    ) -> None:
        if not NAME_RE.match(name):
            raise ValueError(f"tool name {name!r} must be lowercase letters, digits and underscores")
        if name in taken or any(t.name == name for t in added):
            raise ValueError(f"tool name {name!r} is already taken")
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(f"tool {name!r}: model must be a pydantic BaseModel class")
        if not callable(handler):
            raise TypeError(f"tool {name!r}: handler must be callable")
        added.append(ExtraTool(name, str(description).strip(), model, handler, source=str(path)))

    spec = importlib.util.spec_from_file_location(f"praxbot_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        raise TypeError("no register(add) function")
    register(add)
    return added


def load_plugins(home: Path, reserved: set[str], warn: Callable[[str], None]) -> list[ExtraTool]:
    """Tools from every plugin file under home/plugins. A broken plugin is reported and skipped, never fatal."""
    base = home / "plugins"
    if not base.is_dir():
        return []
    tools: list[ExtraTool] = []
    names = set(reserved)
    for path in sorted(base.glob("*.py")):
        try:
            added = _load_one(path, names)
        except Exception as e:
            warn(f"plugin {path.name} skipped: {type(e).__name__}: {str(e)[:200]}")
            continue
        names.update(t.name for t in added)
        tools.extend(added)
    return tools
