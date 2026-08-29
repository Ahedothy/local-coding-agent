"""Validation and local execution boundary for tool calls."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ToolCall

from .base import ToolContext, ToolResult
from .approval import ApprovalGate
from .changes import ChangeStore
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
        approval_gate: ApprovalGate | None = None,
        change_store: ChangeStore | None = None,
    ) -> None:
        self.registry = registry
        self.event_handler = event_handler
        self.approval_gate = approval_gate
        self.change_store = change_store

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

        argument_data = arguments.model_dump(mode="json")
        if (
            self.approval_gate is not None
            and self.approval_gate.requires_approval_for(tool_call.name, argument_data)
        ):
            approval = self.approval_gate.create(tool_call, context, arguments)
            approval_scope = self.approval_gate.auto_approval_scope(context.turn_id)
            automatically_approved = approval_scope is not None
            await self._emit(
                AgentEvent(
                    type=AgentEventType.APPROVAL_REQUESTED,
                    session_id=context.session_id,
                    payload={
                        **approval.as_event_payload(),
                        "automatic": automatically_approved,
                        "approval_scope": approval_scope or "single_action",
                    },
                )
            )
            if automatically_approved:
                self.approval_gate.discard(approval.approval_id)
                decision = "approved"
            else:
                decision = await self.approval_gate.wait(
                    approval,
                    context.cancellation_token,
                )
            if decision == "cancelled":
                self.approval_gate.discard(approval.approval_id)
                raise asyncio.CancelledError
            await self._emit(
                AgentEvent(
                    type=AgentEventType.APPROVAL_RESOLVED,
                    session_id=context.session_id,
                    payload={
                        **approval.as_event_payload(),
                        "decision": decision,
                        "automatic": automatically_approved,
                        "approval_scope": approval_scope
                        or ("current_turn" if approval.approved_for_turn else "single_action"),
                    },
                )
            )
            if decision != "approved":
                return ToolResult(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    success=False,
                    error="operation rejected by user",
                    output={"approval_id": approval.approval_id, "decision": decision},
                    metadata={"approval_id": approval.approval_id, "decision": decision},
                )

        snapshot = None
        if self.change_store is not None:
            try:
                snapshot = self.change_store.capture(tool_call.name, arguments, context)
            except Exception as exc:
                return await self._failure(
                    tool_call,
                    context,
                    f"could not prepare edit snapshot: {exc}",
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

        if self.change_store is not None:
            normalized = self.change_store.record(
                context,
                tool_call.name,
                snapshot,
                normalized,
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
                        "metadata": normalized.metadata,
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

    def finish_turn(self, turn_id: str | None) -> None:
        """Release any turn-scoped approval after the Agent turn completes."""
        if self.approval_gate is not None:
            self.approval_gate.finish_turn(turn_id)

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
