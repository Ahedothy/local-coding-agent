from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coding_agent.models import (
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ToolCall,
)


class FakeCompletions:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def complete(provider: OpenAICompatibleProvider, request: ModelRequest):
    return asyncio.run(provider.complete(request))


def make_provider(completions: FakeCompletions) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        client=FakeClient(completions),
    )


def test_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        OpenAICompatibleProvider()


def test_provider_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(ProviderConfigurationError, match="OPENAI_MODEL"):
        OpenAICompatibleProvider()


def test_provider_maps_request_and_parses_final_response() -> None:
    response = SimpleNamespace(
        id="response-1",
        model="test-model",
        system_fingerprint="fingerprint-1",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="done", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 4}),
    )
    completions = FakeCompletions(response=response)
    provider = make_provider(completions)
    request = ModelRequest(
        messages=[
            ModelMessage(role="system", content="be concise"),
            ModelMessage(role="user", content="inspect"),
        ],
        tools=[
            {
                "name": "read_file",
                "description": "",
                "parameters": {},
            }
        ],
        temperature=0.2,
        max_tokens=100,
    )

    result = complete(provider, request)

    call = completions.calls[0]
    assert call["model"] == "test-model"
    assert call["messages"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "inspect"},
    ]
    assert call["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "",
                "parameters": {},
            },
        }
    ]
    assert call["temperature"] == 0.2
    assert call["max_tokens"] == 100
    assert result.content == "done"
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 4}
    assert result.raw_metadata["id"] == "response-1"


def test_provider_maps_tool_calls_and_tool_results() -> None:
    response = {
        "id": "response-2",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"main.py"}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    completions = FakeCompletions(response=response)
    provider = make_provider(completions)
    request = ModelRequest(
        messages=[
            ModelMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-0",
                        name="list_files",
                        arguments={"path": "."},
                    )
                ],
            ),
            ModelMessage(
                role="tool",
                content='{"success":true}',
                tool_call_id="call-0",
            ),
        ]
    )

    result = complete(provider, request)

    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "main.py"}
    assert completions.calls[0]["messages"] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-0",
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "arguments": '{"path":"."}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"success":true}',
            "tool_call_id": "call-0",
        },
    ]


def test_provider_wraps_api_errors() -> None:
    provider = make_provider(FakeCompletions(error=TimeoutError("timed out")))

    with pytest.raises(ModelProviderError, match="model API request failed"):
        complete(provider, ModelRequest())


def test_provider_rejects_invalid_tool_arguments() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "read_file",
                                "arguments": "not-json",
                            },
                        }
                    ],
                }
            }
        ]
    }
    provider = make_provider(FakeCompletions(response=response))

    with pytest.raises(ModelProviderError, match="not valid JSON"):
        complete(provider, ModelRequest())
