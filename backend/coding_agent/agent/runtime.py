"""Self-written iterative Agent Runtime."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ModelProvider, ModelRequest, ModelResponse
from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolResult
from coding_agent.workspace import Workspace

from .session import Session, SessionStatus
from .termination import AgentLimits, tool_call_signature


EventHandler = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class AgentRunResult:
    """Outcome and counters for one Agent Runtime invocation."""

    status: SessionStatus
    final_answer: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    error: str | None = None


class _AgentCancelled(Exception):
    """Internal control-flow signal for cooperative user cancellation."""


class Agent:
    """Coordinate model calls, context updates, and local tool execution."""

    def __init__(
        self,
        model_provider: ModelProvider,
        tool_executor: ToolExecutor,
        context_manager: ContextManager,
        session: Session,
        *,
        tool_context: ToolContext | None = None,
        limits: AgentLimits | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.model_provider = model_provider
        self.tool_executor = tool_executor
        self.context_manager = context_manager
        self.session = session
        self.limits = limits or AgentLimits()
        self.event_handler = event_handler
        self.tool_context = tool_context or ToolContext(
            session_id=session.session_id,
            workspace=Workspace(session.workspace_root),
        )
        self._cancel_event = asyncio.Event()
        self._iterations = 0
        self._tool_calls = 0

        if self.tool_executor.event_handler is None and event_handler is not None:
            self.tool_executor.event_handler = event_handler

    def cancel(self) -> None:
        """Request cooperative cancellation at the next safe loop boundary."""
        self._cancel_event.set()

    async def run(self, task: str) -> AgentRunResult:
        """Run the Agent loop for one user task."""
        self._iterations = 0
        self._tool_calls = 0
        self.session.task = task
        self.session.status = SessionStatus.RUNNING
        await self._emit(
            AgentEvent(
                type=AgentEventType.SESSION_STARTED,
                session_id=self.session.session_id,
                payload={"workspace_root": str(self.tool_context.workspace.root)},
            )
        )

        if not task.strip():
            result = AgentRunResult(
                status=SessionStatus.FAILED,
                error="task must not be empty",
            )
            return await self._finalize(result)

        truncated = self.context_manager.add_user_message(task)
        await self._emit(
            AgentEvent(
                type=AgentEventType.USER_MESSAGE,
                session_id=self.session.session_id,
                payload={"content": task},
            )
        )
        await self._emit_context_truncated(truncated)

        try:
            async with asyncio.timeout(self.limits.task_timeout_seconds):
                result = await self._run_loop()
        except _AgentCancelled:
            result = AgentRunResult(
                status=SessionStatus.CANCELLED,
                iterations=self._iterations,
                tool_calls=self._tool_calls,
                error="agent run cancelled",
            )
        except asyncio.TimeoutError:
            result = AgentRunResult(
                status=SessionStatus.FAILED,
                iterations=self._iterations,
                tool_calls=self._tool_calls,
                error=f"task timed out after {self.limits.task_timeout_seconds} seconds",
            )
        except asyncio.CancelledError:
            result = AgentRunResult(
                status=SessionStatus.CANCELLED,
                iterations=self._iterations,
                tool_calls=self._tool_calls,
                error="agent task cancelled",
            )
        except Exception as exc:
            result = AgentRunResult(
                status=SessionStatus.FAILED,
                iterations=self._iterations,
                tool_calls=self._tool_calls,
                error=f"agent runtime error: {exc}",
            )
        return await self._finalize(result)

    async def _run_loop(self) -> AgentRunResult:
        parse_errors = 0
        failed_call_counts: defaultdict[str, int] = defaultdict(int)
        same_call_counts: defaultdict[str, int] = defaultdict(int)

        for iteration in range(1, self.limits.max_iterations + 1):
            self._check_cancelled()
            self._iterations = iteration
            await self._emit(
                AgentEvent(
                    type=AgentEventType.ITERATION_STARTED,
                    session_id=self.session.session_id,
                    iteration=iteration,
                )
            )

            request = ModelRequest(
                messages=self.context_manager.build_messages(),
                tools=self.tool_executor.registry.model_schemas(),
            )
            await self._emit(
                AgentEvent(
                    type=AgentEventType.MODEL_REQUEST,
                    session_id=self.session.session_id,
                    iteration=iteration,
                    payload={
                        "message_count": len(request.messages),
                        "tool_schema_count": len(request.tools),
                    },
                )
            )

            try:
                response = await asyncio.wait_for(
                    self.model_provider.complete(request),
                    timeout=self.limits.model_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return self._failed(f"model call timed out after {self.limits.model_timeout_seconds} seconds")
            except Exception as exc:
                return self._failed(f"model provider error: {exc}")

            if not isinstance(response, ModelResponse):
                parse_errors += 1
                if parse_errors >= self.limits.max_consecutive_parse_errors:
                    return self._failed("model returned an invalid response")
                continue

            await self._emit(
                AgentEvent(
                    type=AgentEventType.MODEL_RESPONSE,
                    session_id=self.session.session_id,
                    iteration=iteration,
                    payload={
                        "has_content": bool(response.content),
                        "tool_call_count": len(response.tool_calls),
                    },
                )
            )
            truncated = self.context_manager.add_assistant_message(
                response.content,
                tool_calls=response.tool_calls,
            )
            await self._emit(
                AgentEvent(
                    type=AgentEventType.ASSISTANT_MESSAGE,
                    session_id=self.session.session_id,
                    iteration=iteration,
                    payload={
                        "content": response.content,
                        "tool_call_count": len(response.tool_calls),
                    },
                )
            )
            await self._emit_context_truncated(truncated, iteration)

            if not response.tool_calls:
                if response.content and response.content.strip():
                    return AgentRunResult(
                        status=SessionStatus.COMPLETED,
                        final_answer=response.content,
                        iterations=self._iterations,
                        tool_calls=self._tool_calls,
                    )
                parse_errors += 1
                if parse_errors >= self.limits.max_consecutive_parse_errors:
                    return self._failed("model returned empty content and no tool calls")
                continue

            parse_errors = 0
            calls_to_execute = response.tool_calls[: self.limits.max_tool_calls_per_iteration]
            skipped_calls = response.tool_calls[len(calls_to_execute) :]

            for tool_call in calls_to_execute:
                self._check_cancelled()
                if self._tool_calls >= self.limits.max_total_tool_calls:
                    return self._failed("maximum total tool calls exceeded")
                self._tool_calls += 1
                result = await self.tool_executor.execute(tool_call, self.tool_context)
                await self._append_tool_result(result, iteration)

                signature = tool_call_signature(tool_call)
                same_call_counts[signature] += 1
                if same_call_counts[signature] >= self.limits.max_same_tool_call:
                    return self._failed("maximum repeated identical tool call exceeded")
                if not result.success:
                    failed_call_counts[signature] += 1
                    if failed_call_counts[signature] >= self.limits.max_repeated_failed_tool_call:
                        return self._failed("maximum repeated failed tool call exceeded")

            for tool_call in skipped_calls:
                skipped_result = ToolResult(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    success=False,
                    error=(
                        "tool call skipped because the per-iteration limit was reached"
                    ),
                )
                await self._append_tool_result(skipped_result, iteration)

            if self._tool_calls >= self.limits.max_total_tool_calls:
                return self._failed("maximum total tool calls exceeded")

        return self._failed("maximum iterations exceeded")

    async def _append_tool_result(self, result: ToolResult, iteration: int) -> None:
        truncated = self.context_manager.add_tool_result(result)
        await self._emit_context_truncated(truncated, iteration)

    def _failed(self, error: str) -> AgentRunResult:
        return AgentRunResult(
            status=SessionStatus.FAILED,
            iterations=self._iterations,
            tool_calls=self._tool_calls,
            error=error,
        )

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise _AgentCancelled

    async def _emit_context_truncated(
        self,
        truncated: bool,
        iteration: int | None = None,
    ) -> None:
        if not truncated:
            return
        await self._emit(
            AgentEvent(
                type=AgentEventType.CONTEXT_TRUNCATED,
                session_id=self.session.session_id,
                iteration=iteration,
            )
        )

    async def _finalize(self, result: AgentRunResult) -> AgentRunResult:
        self.session.status = result.status
        if result.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
            await self._emit(
                AgentEvent(
                    type=AgentEventType.AGENT_ERROR,
                    session_id=self.session.session_id,
                    iteration=self._iterations or None,
                    payload={"error": result.error},
                )
            )
        await self._emit(
            AgentEvent(
                type=AgentEventType.AGENT_FINISHED,
                session_id=self.session.session_id,
                iteration=self._iterations or None,
                payload={
                    "status": result.status.value,
                    "iterations": result.iterations,
                    "tool_calls": result.tool_calls,
                    "error": result.error,
                },
            )
        )
        return result

    async def _emit(self, event: AgentEvent) -> None:
        if self.event_handler is None:
            return
        try:
            result = self.event_handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            return
