"""Tool output for the model, with an explicit outcome for the runtime and UIs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolResult:
    output: str
    error: bool = False
