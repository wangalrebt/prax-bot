"""Sampling and reasoning controls: temperature, top_p, and Nemotron's thinking modes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prax_bot.llm import LLM
from prax_bot.settings import Settings
from tests.test_runs import settings as base_settings


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1))


def with_sampling(tmp_path, **kw) -> Settings:
    import dataclasses
    return dataclasses.replace(base_settings(tmp_path), **kw)


def test_default_request_matches_the_frozen_harness(tmp_path) -> None:
    client = FakeClient()
    LLM(with_sampling(tmp_path), client=client).complete([{"role": "user", "content": "hi"}], [])
    call = client.calls[0]
    assert call["temperature"] == 0.2 and "top_p" not in call and "extra_body" not in call


def test_thinking_modes_and_top_p_are_sent_as_nemotron_expects(tmp_path) -> None:
    client = FakeClient()
    LLM(with_sampling(tmp_path, temperature=1.0, top_p=0.95, thinking="low"), client=client).complete([], [])
    call = client.calls[0]
    assert call["temperature"] == 1.0 and call["top_p"] == 0.95
    assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True, "low_effort": True}}
    LLM(with_sampling(tmp_path, thinking="off"), client=client).complete([], [])
    assert client.calls[1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    LLM(with_sampling(tmp_path, thinking="on"), client=client).complete([], [], temperature=0.7)
    assert client.calls[2]["temperature"] == 0.7 and "extra_body" not in client.calls[2]


def test_settings_read_sampling_from_env_and_reject_bad_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXBOT_TEMPERATURE", "1.0")
    monkeypatch.setenv("PRAXBOT_TOP_P", "0.95")
    monkeypatch.setenv("PRAXBOT_THINKING", "LOW")
    s = Settings.from_env()
    assert (s.temperature, s.top_p, s.thinking) == (1.0, 0.95, "low")
    assert Settings.from_env(thinking="off", temperature=0.5, top_p=0.9).thinking == "off"
    monkeypatch.setenv("PRAXBOT_THINKING", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.delenv("PRAXBOT_THINKING")
    monkeypatch.delenv("PRAXBOT_TOP_P")
    monkeypatch.delenv("PRAXBOT_TEMPERATURE")
    d = Settings.from_env()
    assert (d.temperature, d.top_p, d.thinking) == (0.2, None, "on")
