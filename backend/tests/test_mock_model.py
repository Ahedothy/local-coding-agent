from __future__ import annotations

import asyncio

import pytest

from coding_agent.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    MockModelProvider,
    ToolCall,
)


def complete(provider: MockModelProvider, request: ModelRequest) -> ModelResponse:
    return asyncio.run(provider.complete(request))


def test_mock_provider_replays_responses_in_order() -> None:
    provider = MockModelProvider(
        [
            ModelResponse(content="first"),
            ModelResponse(content="second"),
        ]
    )
    request = ModelRequest(messages=[ModelMessage(role="user", content="task")])

    assert complete(provider, request).content == "first"
    assert complete(provider, request).content == "second"
    assert provider.responses_remaining == 0


def test_mock_provider_records_defensive_request_copies() -> None:
    provider = MockModelProvider([ModelResponse(content="done")])
    request = ModelRequest(messages=[ModelMessage(role="user", content="inspect")])

    complete(provider, request)
    request.messages[0].content = "changed after call"

    assert len(provider.requests) == 1
    assert provider.requests[0].messages[0].content == "inspect"


def test_mock_provider_supports_tool_call_and_final_answer_responses() -> None:
    tool_call = ToolCall(id="call-1", name="read_file", arguments={"path": "main.py"})
    provider = MockModelProvider(
        [
            ModelResponse(tool_calls=[tool_call]),
            ModelResponse(content="The task is complete."),
        ]
    )
    request = ModelRequest(messages=[ModelMessage(role="user", content="fix it")])

    tool_response = complete(provider, request)
    final_response = complete(provider, request)

    assert tool_response.tool_calls == [tool_call]
    assert final_response.content == "The task is complete."


def test_mock_provider_supports_an_invalid_empty_response() -> None:
    provider = MockModelProvider([ModelResponse()])

    response = complete(provider, ModelRequest())

    assert response.content is None
    assert response.tool_calls == []


def test_mock_provider_raises_configured_provider_exception() -> None:
    provider = MockModelProvider([TimeoutError("provider timeout")])

    with pytest.raises(TimeoutError, match="provider timeout"):
        complete(provider, ModelRequest())


def test_mock_provider_raises_when_response_queue_is_exhausted() -> None:
    provider = MockModelProvider()

    with pytest.raises(RuntimeError, match="queue exhausted"):
        complete(provider, ModelRequest())
