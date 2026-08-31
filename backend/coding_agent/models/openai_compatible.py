"""OpenAI-compatible model provider adapter.

This module is the only place where the OpenAI SDK is used. The Agent Runtime
receives and returns the provider-neutral models from ``models.base``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from openai import AsyncOpenAI

from .base import ModelMessage, ModelRequest, ModelResponse, ToolCall


class ProviderConfigurationError(ValueError):
    """Raised when the real model provider lacks required configuration."""


class ModelProviderError(RuntimeError):
    """Raised when an API call or provider response cannot be normalized."""

    def __init__(self, message: str, *, retryable: bool = False, reason: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


class OpenAICompatibleProvider:
    """Adapt an OpenAI-compatible Chat Completions API to ``ModelProvider``.

    ``client`` is injectable so parsing and request shaping can be tested
    without making a network call. When omitted, an ``AsyncOpenAI`` client is
    created from environment variables.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or None
        self.model = model or os.getenv("OPENAI_MODEL")
        self.timeout = timeout

        if not self.api_key:
            raise ProviderConfigurationError(
                "OPENAI_API_KEY is required for the real model provider"
            )
        if not self.model:
            raise ProviderConfigurationError(
                "OPENAI_MODEL is required for the real model provider"
            )

        self._client = client or AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send one request and normalize the first returned choice."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_openai(message) for message in request.messages],
        }
        if request.tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in request.tools]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            reason = _retry_reason(exc)
            raise ModelProviderError(
                f"model API request failed: {exc}",
                retryable=reason is not None,
                reason=reason,
            ) from exc

        try:
            return _response_from_openai(response)
        except ModelProviderError as exc:
            raise ModelProviderError(str(exc), retryable=True, reason="invalid_response") from exc
        except Exception as exc:
            raise ModelProviderError(
                f"could not parse model API response: {exc}",
                retryable=True,
                reason="invalid_response",
            ) from exc


def _retry_reason(exc: Exception) -> str | None:
    """Classify transient provider failures without retrying bad requests."""
    name = type(exc).__name__.casefold()
    if "ratelimit" in name or "rate_limit" in name:
        return "rate_limit_429"
    if "connection" in name or "connect" in name or "reset" in name:
        return "connection_error"
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return f"temporary_server_error_{status_code}"
    if "timeout" in name:
        return "provider_timeout"
    return None


def _message_to_openai(message: ModelMessage) -> dict[str, Any]:
    """Convert one internal message to Chat Completions format."""
    result: dict[str, Any] = {"role": message.role}

    if message.role == "tool":
        result["content"] = message.content or ""
        result["tool_call_id"] = message.tool_call_id
        return result

    result["content"] = message.content
    if message.name is not None:
        result["name"] = message.name

    if message.role == "assistant" and message.tool_calls:
        result["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(
                        tool_call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for tool_call in message.tool_calls
        ]
    return result


def _tool_to_openai(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one internal tool schema in Chat Completions function format."""
    try:
        name = tool["name"]
        description = tool.get("description", "")
        parameters = tool["parameters"]
    except KeyError as exc:
        raise ModelProviderError(
            f"tool schema is missing required field: {exc.args[0]}"
        ) from exc
    if not isinstance(name, str) or not name:
        raise ModelProviderError("tool schema name must be a non-empty string")
    if not isinstance(parameters, Mapping):
        raise ModelProviderError("tool schema parameters must be a JSON object")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": dict(parameters),
        },
    }


def _response_from_openai(response: Any) -> ModelResponse:
    choices = _get(response, "choices", [])
    if not choices:
        raise ModelProviderError("model API response contained no choices")

    choice = choices[0]
    message = _get(choice, "message")
    if message is None:
        raise ModelProviderError("model API response choice contained no message")

    tool_calls = [
        _tool_call_from_openai(tool_call)
        for tool_call in (_get(message, "tool_calls", []) or [])
    ]
    content = _normalize_content(_get(message, "content"))
    usage = _to_mapping(_get(response, "usage"))
    raw_metadata = {
        key: value
        for key in ("id", "model", "system_fingerprint")
        if (value := _get(response, key)) is not None
    }
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=_get(choice, "finish_reason"),
        usage=usage,
        raw_metadata=raw_metadata,
    )


def _tool_call_from_openai(tool_call: Any) -> ToolCall:
    call_id = _get(tool_call, "id")
    function = _get(tool_call, "function")
    name = _get(function, "name") if function is not None else None
    arguments = _get(function, "arguments") if function is not None else None

    if not call_id or not name:
        raise ModelProviderError("model returned a tool call without id or name")
    parsed_arguments = _parse_arguments(arguments)
    return ToolCall(id=str(call_id), name=str(name), arguments=parsed_arguments)


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if not isinstance(arguments, str):
        raise ModelProviderError("tool call arguments must be a JSON object")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ModelProviderError("tool call arguments are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelProviderError("tool call arguments must be a JSON object")
    return parsed


def _normalize_content(content: Any) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        text_parts: list[str] = []
        for part in content:
            text = _get(part, "text")
            if text is not None:
                text_parts.append(str(text))
        if text_parts:
            return "".join(text_parts)
    raise ModelProviderError("model message content has an unsupported format")


def _to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)
