"""Simple bounded conversation context for the self-written Agent loop."""

from __future__ import annotations

import json
from collections.abc import Sequence

from coding_agent.models import ModelMessage, ToolCall
from coding_agent.tools import ToolResult

from .messages import message_character_count


TOOL_RESULT_TRUNCATION_MARKER = "\n... [tool result truncated]"


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
    ) -> None:
        if not system_prompt:
            raise ValueError("system_prompt must not be empty")
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than zero")
        if max_tool_result_chars <= len(TOOL_RESULT_TRUNCATION_MARKER):
            raise ValueError("max_tool_result_chars is too small")

        self.max_chars = max_chars
        self.max_tool_result_chars = max_tool_result_chars
        self._messages: list[ModelMessage] = [
            ModelMessage(role="system", content=system_prompt)
        ]
        self.last_truncated = False

    @property
    def messages(self) -> list[ModelMessage]:
        """Return a snapshot of the current messages in conversation order."""
        return [message.model_copy(deep=True) for message in self._messages]

    @property
    def total_chars(self) -> int:
        return sum(message_character_count(message) for message in self._messages)

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
        self._messages.append(message)
        truncated = self._enforce_budget()
        self.last_truncated = truncated
        return truncated

    def _enforce_budget(self) -> bool:
        if self.total_chars <= self.max_chars:
            return False

        system_message = self._messages[0]
        latest_user_index = next(
            (
                index
                for index in range(len(self._messages) - 1, 0, -1)
                if self._messages[index].role == "user"
            ),
            None,
        )
        preserved_indices = {0}
        if latest_user_index is not None:
            preserved_indices.add(latest_user_index)

        kept_indices = set(preserved_indices)
        remaining = self.max_chars - message_character_count(system_message)
        if latest_user_index is not None:
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
        return True

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
