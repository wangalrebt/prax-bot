"""Sampling and reasoning controls: temperature, top_p, and the effort ladder on Nemotron."""

from __future__ import annotations

import pytest

from prax_bot.core.llm import LLM, MAX_COMPLETION_TOKENS, XHIGH_COMPLETION_TOKENS
from prax_bot.settings import Settings
from tests.fakes import FakeClient
from tests.helpers import settings as base_settings


def with_sampling(tmp_path, **kw) -> Settings:
    import dataclasses

    return dataclasses.replace(base_settings(tmp_path), **kw)


def test_frozen_harness_request_is_reproducible(tmp_path) -> None:
    client = FakeClient()
    LLM(with_sampling(tmp_path, temperature=0.2, top_p=None, effort="high"), client=client).complete(
        [{"role": "user", "content": "hi"}], []
    )
    call = client.calls[0]
    assert call["temperature"] == 0.2 and "top_p" not in call and "extra_body" not in call
    assert call["max_tokens"] == MAX_COMPLETION_TOKENS


def test_effort_levels_and_top_p_are_sent_as_nemotron_expects(tmp_path) -> None:
    client = FakeClient()
    LLM(with_sampling(tmp_path, temperature=1.0, top_p=0.95, effort="low"), client=client).complete([], [])
    call = client.calls[0]
    assert call["temperature"] == 1.0 and call["top_p"] == 0.95
    assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True, "low_effort": True}}
    LLM(with_sampling(tmp_path, effort="off"), client=client).complete([], [])
    assert client.calls[1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    LLM(with_sampling(tmp_path, effort="high"), client=client).complete([], [], temperature=0.7)
    assert (
        client.calls[2]["temperature"] == 0.7
        and "extra_body" not in client.calls[2]
        and client.calls[2]["max_tokens"] == MAX_COMPLETION_TOKENS
    )
    LLM(with_sampling(tmp_path, effort="xhigh"), client=client).complete([], [])
    assert "extra_body" not in client.calls[3] and client.calls[3]["max_tokens"] == XHIGH_COMPLETION_TOKENS


def test_settings_read_sampling_from_env_and_reject_bad_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXBOT_TEMPERATURE", "1.0")
    monkeypatch.setenv("PRAXBOT_TOP_P", "0.95")
    monkeypatch.setenv("PRAXBOT_EFFORT", "LOW")
    s = Settings.from_env()
    assert (s.temperature, s.top_p, s.effort) == (1.0, 0.95, "low")
    assert Settings.from_env(effort="xhigh", temperature=0.5, top_p=0.9).effort == "xhigh"
    monkeypatch.setenv("PRAXBOT_EFFORT", "medium")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.delenv("PRAXBOT_EFFORT")
    monkeypatch.delenv("PRAXBOT_TOP_P")
    monkeypatch.delenv("PRAXBOT_TEMPERATURE")
    d = Settings.from_env()
    assert (d.temperature, d.top_p, d.effort) == (1.0, 0.95, "low"), "the measured defaults"
