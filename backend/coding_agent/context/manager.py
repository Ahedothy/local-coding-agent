"""Simple bounded conversation context for the self-written Agent loop."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from coding_agent.models import ModelMessage, ToolCall
from coding_agent.tools import ToolResult

from .messages import message_character_count


TOOL_RESULT_TRUNCATION_MARKER = "\n... [tool result truncated]"


@dataclass(frozen=True, slots=True)
class ContextStats:
    """Deterministic context metrics for runtime events and the Web UI."""

    total_chars: int
    max_chars: int
    message_count: int
    user_message_count: int
    assistant_message_count: int
    tool_message_count: int
    tool_result_chars: int
    summary_chars: int
    compaction_count: int

    @property
    def utilization(self) -> float:
        return self.total_chars / self.max_chars if self.max_chars else 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total_chars": self.total_chars,
            "max_chars": self.max_chars,
            "message_count": self.message_count,
            "user_message_count": self.user_message_count,
            "assistant_message_count": self.assistant_message_count,
            "tool_message_count": self.tool_message_count,
            "tool_result_chars": self.tool_result_chars,
            "summary_chars": self.summary_chars,
            "compaction_count": self.compaction_count,
            "utilization": round(self.utilization, 4),
        }


@dataclass(frozen=True, slots=True)
class ContextChange:
    before_chars: int
    after_chars: int
    messages_compacted: int
    strategy: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "messages_compacted": self.messages_compacted,
            "strategy": self.strategy,
        }


class ContextManager:
    """Maintain provider-neutral messages under a simple character budget.

    The manager deliberately does not summarize messages with another model.
    ``add_*`` methods return True when the operation truncated tool content or
    removed older messages, allowing the caller to emit a context event.
    """

    def __init__(
        self,
        system_prompt: str,
        *,
        max_chars: int = 100_000,
        max_tool_result_chars: int = 20_000,
        recent_message_groups: int = 4,
        enable_compaction: bool = True,
    ) -> None:
        if not system_prompt:
            raise ValueError("system_prompt must not be empty")
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than zero")
        if max_tool_result_chars <= len(TOOL_RESULT_TRUNCATION_MARKER):
            raise ValueError("max_tool_result_chars is too small")
        if recent_message_groups < 0:
            raise ValueError("recent_message_groups must not be negative")

        self.max_chars = max_chars
        self.max_tool_result_chars = max_tool_result_chars
        self.recent_message_groups = recent_message_groups
        self.enable_compaction = enable_compaction
        self._messages: list[ModelMessage] = [
            ModelMessage(role="system", content=system_prompt)
        ]
        self.last_truncated = False
        self.last_change: ContextChange | None = None
        self._summary = ""
        self._compaction_count = 0

    @property
    def messages(self) -> list[ModelMessage]:
        """Return a snapshot of the current messages in conversation order."""
        return [message.model_copy(deep=True) for message in self._messages]

    @property
    def total_chars(self) -> int:
        return sum(message_character_count(message) for message in self._messages)

    @property
    def stats(self) -> ContextStats:
        counts = {"user": 0, "assistant": 0, "tool": 0}
        tool_result_chars = 0
        for message in self._messages:
            if message.role in counts:
                counts[message.role] += 1
            if message.role == "tool":
                tool_result_chars += len(message.content or "")
        return ContextStats(
            total_chars=self.total_chars,
            max_chars=self.max_chars,
            message_count=len(self._messages),
            user_message_count=counts["user"],
            assistant_message_count=counts["assistant"],
            tool_message_count=counts["tool"],
            tool_result_chars=tool_result_chars,
            summary_chars=len(self._summary),
            compaction_count=self._compaction_count,
        )

    def get_messages(self) -> list[ModelMessage]:
        """Return the current model context as a defensive copy."""
        return self.messages

    def build_messages(self) -> list[ModelMessage]:
        """Alias used by callers when constructing a ModelRequest."""
        return self.messages

    def add_user_message(self, content: str) -> bool:
        """Append a user message and return whether context was truncated."""
        return self._append(ModelMessage(role="user", content=content))

    def add_assistant_message(
        self,
        content: str | None = None,
        *,
        tool_calls: Sequence[ToolCall] | None = None,
    ) -> bool:
        """Append an assistant response, optionally containing tool calls."""
        message = ModelMessage(
            role="assistant",
            content=content,
            tool_calls=list(tool_calls or []),
        )
        return self._append(message)

    def add_tool_result(self, result: ToolResult) -> bool:
        """Append a bounded, JSON-encoded tool result as a tool message."""
        content = self._tool_result_content(result)
        content_truncated = len(content) > self.max_tool_result_chars
        if content_truncated:
            content = (
                content[: self.max_tool_result_chars - len(TOOL_RESULT_TRUNCATION_MARKER)]
                + TOOL_RESULT_TRUNCATION_MARKER
            )
        message = ModelMessage(
            role="tool",
            content=content,
            tool_call_id=result.tool_call_id,
            name=result.tool_name,
        )
        if not self._has_matching_tool_call(result.tool_call_id):
            # A previous budget pass may have removed the assistant request.
            # Keeping this result would create an invalid provider context.
            self.last_truncated = True
            self.last_change = ContextChange(
                self.total_chars, self.total_chars, 1, "drop_orphan_tool_result"
            )
            return True
        budget_truncated = self._append(message)
        return content_truncated or budget_truncated

    def _tool_result_content(self, result: ToolResult) -> str:
        payload = {
            "success": result.success,
            "output": result.output,
            "error": result.error,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _has_matching_tool_call(self, tool_call_id: str) -> bool:
        return any(
            message.role == "assistant"
            and any(tool_call.id == tool_call_id for tool_call in message.tool_calls)
            for message in self._messages
        )

    def _append(self, message: ModelMessage) -> bool:
        before = self.total_chars
        self._messages.append(message)
        truncated = self._enforce_budget(before)
        self.last_truncated = truncated
        return truncated

    def _enforce_budget(self, before: int) -> bool:
        if self.total_chars <= self.max_chars:
            self.last_change = None
            return False

        compacted = self._compact_old_groups() if self.enable_compaction else 0
        if compacted and self.total_chars <= self.max_chars:
            self._compaction_count += 1
            self.last_change = ContextChange(
                before, self.total_chars, compacted, "deterministic_extractive_summary"
            )
            return True

        # Fallback for very small budgets: retain valid message groups only.
        latest_user_index = next(
            (
                index
                for index in range(len(self._messages) - 1, 0, -1)
                if self._messages[index].role == "user"
            ),
            None,
        )
        preserved_indices = {0}
        if len(self._messages) > 1 and self._messages[1].role == "system":
            preserved_indices.add(1)
        if latest_user_index is not None:
            preserved_indices.add(latest_user_index)

        kept_indices = set(preserved_indices)
        remaining = self.max_chars - sum(
            message_character_count(self._messages[index])
            for index in preserved_indices
        )
        if latest_user_index is not None and latest_user_index not in preserved_indices:
            remaining -= message_character_count(self._messages[latest_user_index])

        for indices in reversed(self._message_groups()):
            group_indices = set(indices)
            if group_indices & preserved_indices:
                continue
            group_size = sum(
                message_character_count(self._messages[index]) for index in indices
            )
            if group_size <= remaining:
                kept_indices.update(group_indices)
                remaining -= group_size

        self._messages = [
            message for index, message in enumerate(self._messages) if index in kept_indices
        ]
        self.last_change = ContextChange(
            before,
            self.total_chars,
            compacted or 1,
            "summary_then_drop_old_groups" if compacted else "drop_old_groups",
        )
        return True

    def _compact_old_groups(self) -> int:
        groups = self._message_groups()
        if not groups:
            return 0
        recent_groups = groups[-self.recent_message_groups:] if self.recent_message_groups else []
        recent = {tuple(group) for group in recent_groups}
        old_groups = [group for group in groups if tuple(group) not in recent]
        latest_user_index = next(
            (
                index
                for index in range(len(self._messages) - 1, 0, -1)
                if self._messages[index].role == "user"
            ),
            None,
        )
        if latest_user_index is not None:
            old_groups = [group for group in old_groups if latest_user_index not in group]
        if not old_groups:
            return 0
        old_messages = [self._messages[index] for group in old_groups for index in group]
        extracted = self._extract_summary(old_messages)
        if not extracted:
            return 0
        self._summary = ((self._summary + "\n" + extracted).strip())[-2800:]
        summary = ModelMessage(
            role="system", content="[conversation memory]\n" + self._summary
        )
        old_indices = {index for group in old_groups for index in group}
        retained = [
            message for index, message in enumerate(self._messages)
            if index == 0 or index not in old_indices
        ]
        self._messages = [retained[0], summary, *retained[1:]]
        self._fit_summary_to_budget()
        return sum(len(group) for group in old_groups)

    def _fit_summary_to_budget(self) -> None:
        """Trim memory before the old drop-groups fallback is attempted."""
        if len(self._messages) < 2 or self._messages[1].role != "system":
            return
        summary_size = message_character_count(self._messages[1])
        fixed = self.total_chars - summary_size
        available = max(0, self.max_chars - fixed)
        content = (self._messages[1].content or "")[:available]
        self._messages[1] = self._messages[1].model_copy(update={"content": content})
        self._summary = content.removeprefix("[conversation memory]\n")

    def _extract_summary(self, messages: list[ModelMessage]) -> str:
        sections: list[str] = []
        goals = [
            message.content.strip()
            for message in messages
            if message.role == "user" and (message.content or "").strip()
        ]
        if goals:
            sections.append("goals: " + " | ".join(goals[-3:])[:900])
        files: list[str] = []
        commands: list[str] = []
        errors: list[str] = []
        notes: list[str] = []
        for message in messages:
            if message.role == "assistant" and message.content:
                notes.append(message.content.strip())
            if message.role != "tool" or not message.content:
                continue
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            output = payload.get("output") or {}
            if isinstance(output, dict):
                if output.get("path"):
                    files.append(str(output["path"]))
                if output.get("command"):
                    commands.append(str(output["command"]))
                if output.get("stdout") or output.get("stderr"):
                    commands.append(str(output.get("stdout") or output.get("stderr"))[:240])
            if not payload.get("success"):
                errors.append(str(payload.get("error") or "tool failed")[:240])
        if files:
            sections.append("files: " + ", ".join(dict.fromkeys(files))[:800])
        if notes:
            sections.append("notes: " + " | ".join(notes[-2:])[:800])
        if commands:
            sections.append("commands/results: " + " | ".join(commands[-3:])[:900])
        if errors:
            sections.append("unresolved errors: " + " | ".join(dict.fromkeys(errors))[:700])
        return "\n".join(sections)

    def _message_groups(self) -> list[list[int]]:
        """Build atomic groups so tool calls and their results are never split.

        A model assistant message containing tool calls is grouped with the
        following matching tool messages. An orphan tool message is placed in
        a group that can never be retained by the budget algorithm.
        """
        groups: list[list[int]] = []
        index = 1
        while index < len(self._messages):
            message = self._messages[index]
            if message.role == "tool":
                groups.append([])
                index += 1
                continue

            group = [index]
            index += 1
            if message.role == "assistant" and message.tool_calls:
                expected_ids = {tool_call.id for tool_call in message.tool_calls}
                while index < len(self._messages):
                    next_message = self._messages[index]
                    if next_message.role != "tool":
                        break
                    if next_message.tool_call_id not in expected_ids:
                        break
                    group.append(index)
                    index += 1
            groups.append(group)
        return groups
