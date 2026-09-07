"""Plugins: Python files under ~/.praxbot/plugins/ that add tools to the agent.

A plugin defines register(add), and calls add(name, description, model, handler) for each tool: model is a
pydantic class for the arguments, handler(workspace, args) returns the text the model sees. Handlers run on
this machine, not in the sandbox, which is why plugins are loaded only from the home directory and never
from the repository being worked on.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass
class ExtraTool:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[Any, BaseModel], str]
    source: str = ""

    def spec(self) -> dict[str, Any]:
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


def load_plugins(home: Path, reserved: set[str], warn: Callable[[str], None]) -> list[ExtraTool]:
    base = home / "plugins"
    if not base.is_dir():
        return []
    tools: list[ExtraTool] = []
    names = set(reserved)
    for path in sorted(base.glob("*.py")):
        added: list[ExtraTool] = []

        def add(name: str, description: str, model: type[BaseModel], handler: Callable[[Any, BaseModel], str]) -> None:
            if not NAME_RE.match(name):
                raise ValueError(f"tool name {name!r} must be lowercase letters, digits and underscores")
            if name in names:
                raise ValueError(f"tool name {name!r} is already taken")
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise ValueError(f"tool {name!r}: model must be a pydantic BaseModel class")
            if not callable(handler):
                raise ValueError(f"tool {name!r}: handler must be callable")
            names.add(name)
            added.append(ExtraTool(name, str(description).strip(), model, handler, source=str(path)))

        try:
            spec = importlib.util.spec_from_file_location(f"praxbot_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot load")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise ValueError("no register(add) function")
            register(add)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not stop the run
            warn(f"plugin {path.name} skipped: {type(e).__name__}: {str(e)[:200]}")
            for t in added:
                names.discard(t.name)
            continue
        tools.extend(added)
    return tools
