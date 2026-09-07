from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from .settings import Settings


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, model: str, prompt: int, completion: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        entry = self.by_model.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt
        entry["completion_tokens"] += completion


MAX_COMPLETION_TOKENS = 8192  # room for reasoning plus a tool call; the server default can cut a call off


class LLM:
    """Thin wrapper over the OpenAI-compatible Token Factory endpoint."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.model = settings.model
        self.client = client or OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.api_key or "missing",
            max_retries=4,
            timeout=180.0,
        )
        self.usage = Usage()
        self.last_finish_reason: str | None = None

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: Any = "auto",
        temperature: float = 0.2,
        model: str | None = None,
    ) -> Any:
        chosen = model or self.model
        response = self.client.chat.completions.create(
            model=chosen,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=MAX_COMPLETION_TOKENS,
        )
        self.last_finish_reason = getattr(response.choices[0], "finish_reason", None)
        usage = getattr(response, "usage", None)
        self.usage.add(
            chosen,
            (getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0,
            (getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0,
        )
        return response.choices[0].message

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list())
