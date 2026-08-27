"""Small helpers for representing model-facing message content."""

from __future__ import annotations

import json

from coding_agent.models import ModelMessage


def message_character_count(message: ModelMessage) -> int:
    """Estimate context size using characters, including tool call arguments."""
    count = len(message.content or "")
    for tool_call in message.tool_calls:
        count += len(
            json.dumps(
                tool_call.model_dump(mode="json"),
                ensure_ascii=False,
                default=str,
            )
        )
    return count
