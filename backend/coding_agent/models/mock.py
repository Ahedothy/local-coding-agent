"""Deterministic model provider used by Agent Core tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Protocol

from .base import ModelRequest, ModelResponse


class ModelProvider(Protocol):
    """Provider-neutral interface consumed by the future Agent Runtime."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one normalized model response for a request."""


MockResponse = ModelResponse | BaseException


class MockModelProvider:
    """Replay configured responses and record every request it receives.

    A response queue item may be a ``ModelResponse`` or an exception instance.
    Exceptions are raised by ``complete`` so Agent Runtime tests can exercise
    provider failure paths without a network call.
    """

    def __init__(self, responses: Iterable[MockResponse] = ()) -> None:
        self._responses: deque[MockResponse] = deque(responses)
        self.requests: list[ModelRequest] = []

    @property
    def responses_remaining(self) -> int:
        return len(self._responses)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Record a defensive request copy and replay the next queue item."""
        self.requests.append(request.model_copy(deep=True))
        if not self._responses:
            raise RuntimeError("MockModelProvider response queue exhausted")

        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response.model_copy(deep=True)

    def add_response(self, response: MockResponse) -> None:
        """Append one response or exception to the replay queue."""
        self._responses.append(response)
