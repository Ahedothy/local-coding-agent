"""Validation and local execution boundary for tool calls."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ToolCall

from .base import ToolContext, ToolResult
from .registry import ToolRegistry


EventHandler = Callable[[AgentEvent], Awaitable[None] | None]


def _format_execution_error(exc: Exception) -> str:
    """Keep the exception type visible even when its message is empty."""
    message = str(exc)
    if message:
        return f"tool execution failed: {message}"
    return f"tool execution failed: {type(exc).__name__}: {exc!r}"


class ToolExecutor:
    """Execute registered tools locally and normalize every outcome."""

    def __init__(
        self,
        registry: ToolRegistry,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.registry = registry
        self.event_handler = event_handler

    async def execute(self, tool_call: ToolCall, context: ToolContext) -> ToolResult:
        """Validate and execute one model-produced tool call."""
        tool = self.registry.get(tool_call.name)
        if tool is None:
            return await self._failure(
                tool_call,
                context,
                f"unknown tool: {tool_call.name}",
                emit_started=False,
            )

        try:
            arguments = tool.parameters_model.model_validate(tool_call.arguments)
        except ValidationError as exc:
            return await self._failure(
                tool_call,
                context,
                f"invalid arguments: {exc}",
                emit_started=False,
            )

        await self._emit(
            AgentEvent(
                type=AgentEventType.TOOL_STARTED,
                session_id=context.session_id,
                payload={"tool_name": tool_call.name, "tool_call_id": tool_call.id},
            )
        )

        started_at = time.monotonic()
        try:
            result = await tool.execute(context, arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("tool must return ToolResult")
            normalized = result.model_copy(
                update={"tool_call_id": tool_call.id, "tool_name": tool_call.name}
            )
        except Exception as exc:
            return await self._failure(
                tool_call,
                context,
                _format_execution_error(exc),
                emit_started=False,
                duration_seconds=time.monotonic() - started_at,
            )

        duration_seconds = time.monotonic() - started_at
        if normalized.success:
            await self._emit(
                AgentEvent(
                    type=AgentEventType.TOOL_FINISHED,
                    session_id=context.session_id,
                    payload={
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "success": True,
                        "output": normalized.output,
                        "duration_seconds": duration_seconds,
                    },
                )
            )
        else:
            await self._emit_failure_event(
                tool_call,
                context,
                normalized.error or "tool failed",
                output=normalized.output,
                duration_seconds=duration_seconds,
            )
        return normalized

    async def _failure(
        self,
        tool_call: ToolCall,
        context: ToolContext,
        error: str,
        *,
        emit_started: bool = True,
        duration_seconds: float | None = None,
    ) -> ToolResult:
        if emit_started:
            await self._emit(
                AgentEvent(
                    type=AgentEventType.TOOL_STARTED,
                    session_id=context.session_id,
                    payload={"tool_name": tool_call.name, "tool_call_id": tool_call.id},
                )
            )
        await self._emit_failure_event(
            tool_call,
            context,
            error,
            duration_seconds=duration_seconds,
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=False,
            error=error,
        )

    async def _emit_failure_event(
        self,
        tool_call: ToolCall,
        context: ToolContext,
        error: str,
        output: Any | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        await self._emit(
            AgentEvent(
                type=AgentEventType.TOOL_FAILED,
                session_id=context.session_id,
                payload={
                    "tool_name": tool_call.name,
                    "tool_call_id": tool_call.id,
                    "error": error,
                    "success": False,
                    "output": output,
                    "duration_seconds": duration_seconds,
                },
            )
        )

    async def _emit(self, event: AgentEvent) -> None:
        if self.event_handler is None:
            return
        try:
            result = self.event_handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Observability must not turn a valid local tool result into a failure.
            return
