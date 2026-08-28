"""User approval gate for local tools with side effects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from coding_agent.models import ToolCall

from .base import ToolContext


APPROVAL_REQUIRED_TOOLS = frozenset(
    {
        "execute_command",
        "write_file",
        "replace_in_file",
        "apply_patch",
    }
)


def _preview(value: object, max_chars: int = 12_000) -> str:
    text = str(value)
    return text if len(text) <= max_chars else f"{text[:max_chars]}\n... (truncated)"


def _approval_kind(tool_name: str) -> str:
    return "command" if tool_name == "execute_command" else "edit"


def _approval_details(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "execute_command":
        return {
            "command": arguments.get("command", []),
            "cwd": arguments.get("cwd", "."),
            "timeout_seconds": arguments.get("timeout_seconds", 30.0),
        }
    if tool_name == "apply_patch":
        return {"patch": _preview(arguments.get("patch", ""))}
    if tool_name == "replace_in_file":
        return {
            "path": arguments.get("path", ""),
            "old_text": _preview(arguments.get("old_text", ""), 4_000),
            "new_text": _preview(arguments.get("new_text", ""), 4_000),
            "expected_replacements": arguments.get("expected_replacements", 1),
        }
    if tool_name == "write_file":
        content = str(arguments.get("content", ""))
        return {
            "path": arguments.get("path", ""),
            "content_preview": _preview(content, 8_000),
            "content_chars": len(content),
            "overwrite": arguments.get("overwrite", True),
            "create_parent_dirs": arguments.get("create_parent_dirs", False),
        }
    return {"arguments": arguments}


@dataclass(slots=True)
class ApprovalRequest:
    """One pending approval that blocks a tool until the user decides."""

    approval_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    kind: str
    summary: str
    details: dict[str, Any]
    turn_id: str | None = None
    approved_for_turn: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    future: asyncio.Future[str] | None = field(default=None, repr=False)

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "kind": self.kind,
            "summary": self.summary,
            "details": self.details,
            "turn_id": self.turn_id,
            "created_at": self.created_at.isoformat(),
        }


class ApprovalGate:
    """Coordinate explicit approve/reject decisions for local side effects."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._auto_approved_turns: set[str] = set()
        self._auto_approval_enabled = False

    @staticmethod
    def requires_approval(tool_name: str) -> bool:
        return tool_name in APPROVAL_REQUIRED_TOOLS

    def create(
        self,
        tool_call: ToolCall,
        context: ToolContext,
        arguments: Any,
    ) -> ApprovalRequest:
        argument_data = arguments.model_dump(mode="json")
        request = ApprovalRequest(
            approval_id=str(uuid4()),
            session_id=context.session_id,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            kind=_approval_kind(tool_call.name),
            summary=(
                "Run a local command"
                if tool_call.name == "execute_command"
                else "Apply local file changes"
            ),
            details=_approval_details(tool_call.name, argument_data),
            turn_id=context.turn_id,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request.approval_id] = request
        return request

    def enable_auto_approval(self, turn_id: str | None) -> None:
        """Allow side-effecting tools for one active Agent turn."""
        if turn_id:
            self._auto_approved_turns.add(turn_id)

    def set_auto_approval(self, enabled: bool) -> None:
        """Set the session-level approval mode for subsequently started turns."""
        self._auto_approval_enabled = enabled

    def auto_approval_scope(self, turn_id: str | None) -> str | None:
        if self._auto_approval_enabled:
            return "session"
        if turn_id and turn_id in self._auto_approved_turns:
            return "current_turn"
        return None

    def is_auto_approved(self, turn_id: str | None) -> bool:
        return self.auto_approval_scope(turn_id) is not None

    def finish_turn(self, turn_id: str | None) -> None:
        """Release turn-scoped approval as soon as the turn finishes."""
        if turn_id:
            self._auto_approved_turns.discard(turn_id)

    async def wait(
        self,
        request: ApprovalRequest,
        cancellation_token: Any = None,
    ) -> str:
        if request.future is None:
            raise RuntimeError("approval request is not awaitable")
        if not isinstance(cancellation_token, asyncio.Event):
            return await request.future

        cancellation_wait = asyncio.create_task(cancellation_token.wait())
        try:
            done, _ = await asyncio.wait(
                {request.future, cancellation_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_wait in done and cancellation_token.is_set():
                return "cancelled"
            return request.future.result()
        finally:
            cancellation_wait.cancel()
            await asyncio.gather(cancellation_wait, return_exceptions=True)

    def decide(
        self,
        approval_id: str,
        approved: bool,
        approve_current_turn: bool = False,
    ) -> ApprovalRequest | None:
        request = self._pending.get(approval_id)
        if request is None or request.future is None or request.future.done():
            return None
        if approved and approve_current_turn:
            request.approved_for_turn = True
            self.enable_auto_approval(request.turn_id)
        request.future.set_result("approved" if approved else "rejected")
        self._pending.pop(approval_id, None)
        return request

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def has_pending(self, approval_id: str) -> bool:
        return approval_id in self._pending
