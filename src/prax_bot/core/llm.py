"""The model client: one place that talks to Token Factory's OpenAI-compatible API and counts tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from openai import OpenAI

from prax_bot.settings import Settings


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


MAX_COMPLETION_TOKENS = 16384  # room for reasoning plus a tool call; a cut-off reply wastes the whole turn
XHIGH_COMPLETION_TOKENS = 32768  # NVIDIA's own agent configuration for Nemotron 3 Super


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
        self.last_prompt_tokens = 0  # the context the most recent call sent, in tokens
        self.last_completion_tokens = 0
        self.temperature = settings.temperature
        self.top_p = settings.top_p
        self.effort = settings.effort

    def request_options(self) -> dict[str, Any]:
        """Sampling and reasoning options for one request. Effort 'high' sends nothing extra: the model's default."""
        options: dict[str, Any] = {
            "max_tokens": XHIGH_COMPLETION_TOKENS if self.effort == "xhigh" else MAX_COMPLETION_TOKENS
        }
        if self.top_p is not None:
            options["top_p"] = self.top_p
        if self.effort == "low":
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True, "low_effort": True}}
        elif self.effort == "off":
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return options

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        tool_choice: Any = "auto",
        temperature: float | None = None,
        model: str | None = None,
    ) -> Any:
        """One chat completion. With no tools (a summary, a probe) the request carries no tool fields at all."""
        chosen = model or self.model
        tool_fields = {"tools": tools, "tool_choice": tool_choice} if tools else {}
        response = self.client.chat.completions.create(
            model=chosen,
            messages=cast(Any, messages),  # plain dicts in the OpenAI wire shape; the SDK's TypedDicts add nothing here
            temperature=self.temperature if temperature is None else temperature,
            **tool_fields,
            **self.request_options(),
        )
        self.last_finish_reason = getattr(response.choices[0], "finish_reason", None)
        usage = getattr(response, "usage", None)
        prompt = (getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0
        completion = (getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0
        self.last_prompt_tokens, self.last_completion_tokens = prompt, completion
        self.usage.add(chosen, prompt, completion)
        return response.choices[0].message

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list())
