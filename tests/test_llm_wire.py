"""SDK serialization and parsing against a mock HTTP transport, not a hosted model."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
from openai import OpenAI

from prax_bot.core.agent import assistant_message
from prax_bot.core.llm import LLM, MAX_COMPLETION_TOKENS
from prax_bot.settings import DEFAULT_MODEL
from tests.helpers import settings as base_settings


def test_sdk_tool_round_trip_serializes_nemotron_35_options(tmp_path: Path) -> None:
    requests = []
    tool_call = {
        "id": "call_ping",
        "type": "function",
        "function": {"name": "ping", "arguments": '{"text":"pong"}'},
    }
    response_messages = [
        {"role": "assistant", "content": None, "reasoning_content": "mock reasoning", "tool_calls": [tool_call]},
        {"role": "assistant", "content": "pong"},
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        index = len(requests)
        requests.append(json.loads(request.content))
        prompt_tokens, completion_tokens = [(100, 20), (140, 10)][index]
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-mock-{index}",
                "object": "chat.completion",
                "created": 0,
                "model": DEFAULT_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": response_messages[index],
                        "finish_reason": "tool_calls" if index == 0 else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Reply with the given text.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    forced_choice = {"type": "function", "function": {"name": "ping"}}
    messages = [{"role": "user", "content": "Call ping with text pong."}]
    settings = dataclasses.replace(base_settings(tmp_path), model=DEFAULT_MODEL, effort="low")
    with OpenAI(
        api_key="fake-key",
        base_url="https://tokenfactory.example.invalid/v1/",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        llm = LLM(settings, client=client)
        first = llm.complete(messages, tools, tool_choice=forced_choice)
        assert len(first.tool_calls) == 1
        call = first.tool_calls[0]
        assert call.id == "call_ping" and call.function.name == "ping"
        assert json.loads(call.function.arguments) == {"text": "pong"}
        assert llm.last_finish_reason == "tool_calls"
        messages.append(assistant_message(first))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": "pong"})
        final = llm.complete(messages, None, tool_choice=forced_choice)

    assert final.content == "pong" and not final.tool_calls
    assert llm.last_finish_reason == "stop" and llm.last_prompt_tokens == 140
    assert (llm.usage.calls, llm.usage.prompt_tokens, llm.usage.completion_tokens) == (2, 240, 30)
    assert llm.usage.by_model == {DEFAULT_MODEL: {"calls": 2, "prompt_tokens": 240, "completion_tokens": 30}}
    assert len(requests) == 2
    for body in requests:
        assert body["model"] == DEFAULT_MODEL
        assert body["chat_template_kwargs"] == {"enable_thinking": True, "medium_effort": True}
        assert "extra_body" not in body and "low_effort" not in body["chat_template_kwargs"]
        assert body["max_tokens"] == MAX_COMPLETION_TOKENS
    assert requests[0]["tools"] == tools and requests[0]["tool_choice"] == forced_choice
    assert requests[1]["messages"] == [
        messages[0],
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call_ping", "content": "pong"},
    ]
    assert "tools" not in requests[1] and "tool_choice" not in requests[1]
