"""Self-written iterative Agent Runtime."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ModelMessage, ModelProvider, ModelRequest, ModelResponse
from coding_agent.models.openai_compatible import ModelProviderError
from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolResult
from coding_agent.workspace import Workspace

from .session import Session, SessionStatus
from .termination import AgentLimits, tool_call_signature
from .verification import VerificationLedger
from .instructions import WorkspaceInstructions


EventHandler = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class AgentRunResult:
    """Outcome and counters for one Agent Runtime invocation."""

    status: SessionStatus
    final_answer: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    error: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)


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
        workspace_instructions: WorkspaceInstructions | None = None,
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
        self._turn_lock = asyncio.Lock()
        self._turn_index = 0
        self._session_started = False
        self._iterations = 0
        self._tool_calls = 0
        self._plan_emitted = False
        self._assistant_message_emitted = False
        self._turn_started_at: float | None = None
        self._turn_diffs: dict[str, str] = {}
        self.verification = VerificationLedger()
        self.workspace_instructions = workspace_instructions

        if self.tool_executor.event_handler is None and event_handler is not None:
            self.tool_executor.event_handler = event_handler
        self.tool_context.cancellation_token = self._cancel_event

    def cancel(self) -> None:
        """Request cooperative cancellation at the next safe loop boundary."""
        self._cancel_event.set()

    @property
    def turn_index(self) -> int:
        """Return the next conversation turn number that has been allocated."""
        return self._turn_index

    def restore_runtime_state(self, state: object) -> None:
        """Restore small runtime counters without restoring live execution."""
        if not isinstance(state, dict):
            return
        raw_turn_index = state.get("turn_index", 0)
        try:
            self._turn_index = max(0, int(raw_turn_index))
        except (TypeError, ValueError):
            self._turn_index = 0
        if isinstance(state, dict):
            self.verification = VerificationLedger.from_state(state.get("verification"))

    def runtime_state(self) -> dict[str, Any]:
        """Return bounded runtime state used by local history persistence."""
        return {
            "turn_index": self._turn_index,
            "verification": self.verification.to_state(),
        }

    async def run(self, task: str) -> AgentRunResult:
        """Run one task, preserving the historical one-shot API."""
        return await self.run_turn(task)

    async def run_turn(self, user_message: str) -> AgentRunResult:
        """Run one user turn while preserving the session conversation."""
        if self._turn_lock.locked():
            return AgentRunResult(
                status=SessionStatus.FAILED,
                error="agent is already running a turn",
            )

        async with self._turn_lock:
            return await self._run_turn_locked(user_message)

    async def _run_turn_locked(self, user_message: str) -> AgentRunResult:
        """Execute a turn after exclusive ownership has been acquired."""
        self._iterations = 0
        self._tool_calls = 0
        self._plan_emitted = False
        self._assistant_message_emitted = False
        self._turn_diffs = {}
        self._turn_started_at = time.monotonic()
        self._turn_index += 1
        self.tool_context.turn_id = f"turn-{self._turn_index}"
        self.session.task = user_message
        self.session.status = SessionStatus.RUNNING
        self.session.updated_at = datetime.now(timezone.utc)
        if not self._session_started:
            self._session_started = True
            await self._emit(
                AgentEvent(
                    type=AgentEventType.SESSION_STARTED,
                    session_id=self.session.session_id,
                    payload={"workspace_root": str(self.tool_context.workspace.root)},
                )
            )
            instructions = self.workspace_instructions
            if instructions is not None:
                await self._emit(
                    AgentEvent(
                        type=AgentEventType.WORKSPACE_INSTRUCTIONS_LOADED,
                        session_id=self.session.session_id,
                        payload={
                            "path": instructions.path,
                            "found": instructions.found,
                            "injected_chars": instructions.injected_chars,
                            "truncated": instructions.truncated,
                        },
                    )
                )

        if not user_message.strip():
            result = AgentRunResult(
                status=SessionStatus.FAILED,
                error="task must not be empty",
            )
            try:
                return await self._finalize(result)
            finally:
                self._cancel_event.clear()

        truncated = self.context_manager.add_user_message(user_message)
        await self._emit(
            AgentEvent(
                type=AgentEventType.USER_MESSAGE,
                session_id=self.session.session_id,
                payload={"content": user_message, "turn_index": self._turn_index},
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
        try:
            return await self._finalize(result)
        finally:
            # Cancellation belongs to the active turn and must not poison the next one.
            self.tool_executor.finish_turn(self.tool_context.turn_id)
            self._cancel_event.clear()

    async def _run_loop(self) -> AgentRunResult:
        parse_errors = 0
        failed_call_counts: defaultdict[str, int] = defaultdict(int)
        same_call_counts: defaultdict[str, int] = defaultdict(int)
        # A stale-file race can produce different tool-call signatures on each
        # retry (for example, the model re-reads the file and changes the hash).
        # Track the condition independently of the exact signature so the run
        # stops with a useful explanation instead of exhausting iterations.
        stale_file_failures = 0

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
                        "context": self.context_manager.stats.as_dict(),
                    },
                )
            )
            model_started_at = time.monotonic()
            response = None
            max_model_attempts = self.limits.max_model_retries + 1
            for attempt in range(1, max_model_attempts + 1):
                try:
                    response = await asyncio.wait_for(
                        self.model_provider.complete(request),
                        timeout=self.limits.model_timeout_seconds,
                    )
                    break
                except asyncio.TimeoutError as exc:
                    reason = "model_timeout"
                    if attempt >= max_model_attempts:
                        return self._failed(f"model call timed out after {self.limits.model_timeout_seconds} seconds")
                    await self._schedule_model_retry(iteration, attempt + 1, reason, 0.5 if attempt == 1 else 1.5)
                    await asyncio.sleep(0.5 if attempt == 1 else 1.5)
                except ModelProviderError as exc:
                    if not exc.retryable or attempt >= max_model_attempts:
                        return self._failed(f"model provider error: {exc}")
                    delay = 0.5 if attempt == 1 else 1.5
                    await self._schedule_model_retry(iteration, attempt + 1, exc.reason or "transient_provider_error", delay)
                    await asyncio.sleep(delay)
                except Exception as exc:
                    return self._failed(f"model provider error: {exc}")

            if not isinstance(response, ModelResponse):
                parse_errors += 1
                if parse_errors >= self.limits.max_consecutive_parse_errors:
                    return self._failed("model returned an invalid response")
                delay = 0.5
                await self._schedule_model_retry(iteration, parse_errors + 1, "invalid_response", delay)
                await asyncio.sleep(delay)
                continue

            await self._emit(
                AgentEvent(
                    type=AgentEventType.MODEL_RESPONSE,
                    session_id=self.session.session_id,
                    iteration=iteration,
                    payload={
                        "has_content": bool(response.content),
                        "tool_call_count": len(response.tool_calls),
                        "finish_reason": response.finish_reason,
                        "usage": response.usage,
                        "model_metadata": response.raw_metadata,
                        "duration_seconds": time.monotonic() - model_started_at,
                    },
                )
            )
            truncated = self.context_manager.add_assistant_message(
                response.content,
                tool_calls=response.tool_calls,
            )
            final_content = response.content
            if not response.tool_calls and response.content and self._turn_diffs:
                combined_diff = "\n".join(diff.rstrip("\n") for diff in self._turn_diffs.values())
                final_content = (
                    f"{response.content.rstrip()}\n\n"
                    "## Changes\n\n"
                    f"```diff\n{combined_diff}\n```"
                )
            await self._emit(
                AgentEvent(
                    type=AgentEventType.ASSISTANT_MESSAGE,
                    session_id=self.session.session_id,
                    iteration=iteration,
                    payload={
                        "content": final_content,
                        "tool_call_count": len(response.tool_calls),
                        "has_changes": bool(self._turn_diffs),
                    },
                )
            )
            if not response.tool_calls and response.content and response.content.strip():
                self._assistant_message_emitted = True
            if response.content and response.content.strip() and response.tool_calls:
                is_initial_plan = not self._plan_emitted
                await self._emit(
                    AgentEvent(
                        type=(
                            AgentEventType.PLAN
                            if is_initial_plan
                            else AgentEventType.REFLECTION
                        ),
                        session_id=self.session.session_id,
                        iteration=iteration,
                        payload={
                            "content": response.content,
                            "turn_index": self._turn_index,
                            "tool_call_count": len(response.tool_calls),
                        },
                    )
                )
                self._plan_emitted = True
            await self._emit_context_truncated(truncated, iteration)

            if not response.tool_calls:
                if response.content and response.content.strip():
                    return AgentRunResult(
                        status=SessionStatus.COMPLETED,
                        final_answer=final_content,
                        iterations=self._iterations,
                        tool_calls=self._tool_calls,
                        verification=self.verification.summary(),
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
                    verified = await self._verified_completion()
                    return verified or self._failed("maximum total tool calls exceeded")
                self._tool_calls += 1
                result = await self.tool_executor.execute(tool_call, self.tool_context)
                await self._append_tool_result(result, iteration)
                if isinstance(result.output, dict):
                    diff = result.output.get("diff")
                    if result.success and isinstance(diff, str) and diff.strip():
                        change_id = result.metadata.get("change_id")
                        key = change_id if isinstance(change_id, str) else f"diff-{len(self._turn_diffs)}"
                        self._turn_diffs[key] = diff
                        self.verification.mark_modified()
                        await self._emit_verification_update(
                            iteration,
                            reason="workspace_modified",
                        )

                    verification = result.output.get("verification")
                    if isinstance(verification, dict):
                        evidence = self._record_verification(result, verification)
                        if evidence is not None:
                            await self._emit_verification_update(
                                iteration,
                                reason="command_completed",
                                evidence=evidence.as_dict(),
                            )

                if not result.success and "stale file" in (result.error or ""):
                    stale_file_failures += 1
                    if stale_file_failures >= self.limits.max_same_tool_call:
                        return self._failed(
                            "file changed repeatedly while Agent was editing; "
                            "stopped to avoid overwriting newer changes. "
                            "Please check the file and start the edit again."
                        )
                elif result.success and result.metadata.get("change_id"):
                    # A successful write means the race has been resolved for
                    # the current run; subsequent stale failures are a new
                    # incident rather than an extension of the old one.
                    stale_file_failures = 0

                if (
                    not result.success
                    and result.metadata.get("decision") == "rejected"
                ):
                    return self._failed("operation rejected by user; Agent stopped without retrying it")

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
                verified = await self._verified_completion()
                return verified or self._failed("maximum total tool calls exceeded")

        if stale_file_failures > 0:
            return self._failed(
                "file kept changing while Agent was editing; "
                "stopped before overwriting newer content. "
                "Please check the file and start the edit again."
            )
        verified = await self._verified_completion()
        if verified is not None:
            return verified
        return self._failed("maximum iterations exceeded")

    async def _schedule_model_retry(
        self,
        iteration: int,
        attempt: int,
        reason: str,
        delay_seconds: float,
    ) -> None:
        await self._emit(
            AgentEvent(
                type=AgentEventType.MODEL_RETRY_SCHEDULED,
                session_id=self.session.session_id,
                iteration=iteration,
                payload={
                    "attempt": attempt,
                    "reason": reason,
                    "delay_seconds": delay_seconds,
                },
            )
        )

    def _record_verification(self, result: ToolResult, metadata: dict[str, Any]):
        """Record only evidence observed from a local command result."""
        kind = metadata.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        output = result.output if isinstance(result.output, dict) else {}
        return self.verification.record(
            kind=kind,
            command=(
                [str(part) for part in output.get("command", [])]
                if isinstance(output.get("command"), list)
                else None
            ),
            criteria=(
                [str(item) for item in metadata.get("criteria", [])]
                if isinstance(metadata.get("criteria"), list)
                else []
            ),
            success=result.success and output.get("returncode") == 0,
            returncode=(
                int(output["returncode"])
                if isinstance(output.get("returncode"), int)
                else None
            ),
            summary=self._verification_summary(result, output),
            duration_seconds=(
                float(output["duration_seconds"])
                if isinstance(output.get("duration_seconds"), (int, float))
                else None
            ),
        )

    @staticmethod
    def _verification_summary(result: ToolResult, output: dict[str, Any]) -> str:
        text = str(output.get("stdout") or output.get("stderr") or "").strip()
        if text:
            return text
        if result.error:
            return result.error
        return f"command completed with returncode={output.get('returncode')}"

    async def _emit_verification_update(
        self,
        iteration: int | None,
        *,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        await self._emit(
            AgentEvent(
                type=AgentEventType.VERIFICATION_UPDATED,
                session_id=self.session.session_id,
                iteration=iteration,
                payload={
                    "reason": reason,
                    "evidence": evidence,
                    "verification": self.verification.summary(),
                },
            )
        )

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

    async def _verified_completion(self) -> AgentRunResult | None:
        """Return a successful result when verification already proves completion."""
        verification = self.verification.summary()
        if verification.get("status") not in {"verified", "partially_verified"}:
            return None
        final_answer = await self._generate_verified_final_answer(verification)
        return AgentRunResult(
            status=SessionStatus.COMPLETED,
            final_answer=final_answer or "任务已完成。已生成并验证本次修改。",
            iterations=self._iterations,
            tool_calls=self._tool_calls,
            verification=verification,
        )

    async def _generate_verified_final_answer(
        self,
        verification: dict[str, Any],
    ) -> str | None:
        """Ask the model for a real final answer once local evidence proves completion."""
        summary_prompt = (
            "本地工具已经完成必要修改，并且验证账本显示当前任务可以收尾。\n"
            "请生成面向用户的最终回复，简洁说明完成了什么、验证了什么，"
            "如果仍有未覆盖的风险也请如实说明。不要再调用工具。"
        )
        request = ModelRequest(
            messages=[
                *self.context_manager.build_messages(),
                ModelMessage(
                    role="user",
                    content=(
                        f"{summary_prompt}\n\n"
                        f"验证摘要：{verification}"
                    ),
                ),
            ],
            tools=[],
        )
        try:
            response = await asyncio.wait_for(
                self.model_provider.complete(request),
                timeout=self.limits.model_timeout_seconds,
            )
        except Exception:
            return None
        if response.tool_calls or not response.content or not response.content.strip():
            return None
        truncated = self.context_manager.add_assistant_message(response.content)
        self._assistant_message_emitted = True
        self._iterations += 1
        await self._emit(
            AgentEvent(
                type=AgentEventType.MODEL_RESPONSE,
                session_id=self.session.session_id,
                iteration=self._iterations,
                payload={
                    "has_content": True,
                    "tool_call_count": 0,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                    "model_metadata": response.raw_metadata,
                    "verified_final_answer": True,
                },
            )
        )
        await self._emit(
            AgentEvent(
                type=AgentEventType.ASSISTANT_MESSAGE,
                session_id=self.session.session_id,
                iteration=self._iterations,
                payload={
                    "content": response.content,
                    "tool_call_count": 0,
                    "has_changes": bool(self._turn_diffs),
                    "verified_final_answer": True,
                },
            )
        )
        await self._emit_context_truncated(truncated, self._iterations)
        return response.content

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
                type=(
                    AgentEventType.CONTEXT_COMPACTED
                    if self.context_manager.last_change is not None
                    and "summary" in self.context_manager.last_change.strategy
                    else AgentEventType.CONTEXT_TRUNCATED
                ),
                session_id=self.session.session_id,
                iteration=iteration,
                payload={
                    **(
                        self.context_manager.last_change.as_dict()
                        if self.context_manager.last_change is not None
                        else {}
                    ),
                    "context": self.context_manager.stats.as_dict(),
                },
            )
        )

    async def _finalize(self, result: AgentRunResult) -> AgentRunResult:
        self.session.status = SessionStatus.IDLE
        self.session.updated_at = datetime.now(timezone.utc)
        result.verification = self.verification.summary()
        duration_seconds = (
            time.monotonic() - self._turn_started_at
            if self._turn_started_at is not None
            else None
        )
        if result.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
            await self._emit(
                AgentEvent(
                    type=AgentEventType.AGENT_ERROR,
                    session_id=self.session.session_id,
                    iteration=self._iterations or None,
                    payload={
                        "error": result.error,
                        "turn_index": self._turn_index,
                        "duration_seconds": duration_seconds,
                    },
                )
            )
        elif result.status is SessionStatus.COMPLETED and result.final_answer and not self._assistant_message_emitted:
            await self._emit(
                AgentEvent(
                    type=AgentEventType.ASSISTANT_MESSAGE,
                    session_id=self.session.session_id,
                    iteration=self._iterations or None,
                    payload={
                        "content": result.final_answer,
                        "tool_call_count": 0,
                        "has_changes": bool(self._turn_diffs),
                        "synthetic": True,
                    },
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
                    "turn_index": self._turn_index,
                    "duration_seconds": duration_seconds,
                    "verification": result.verification,
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
