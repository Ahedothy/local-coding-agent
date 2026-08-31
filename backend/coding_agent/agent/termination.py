"""Explicit limits and signatures used by the Agent Runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass

from coding_agent.models import ToolCall


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """Safety limits that prevent an Agent run from continuing indefinitely."""

    max_iterations: int = 10
    max_total_tool_calls: int = 25
    max_tool_calls_per_iteration: int = 3
    max_consecutive_parse_errors: int = 2
    max_model_retries: int = 2
    max_repeated_failed_tool_call: int = 2
    max_same_tool_call: int = 3
    task_timeout_seconds: float = 300.0
    model_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        positive_integer_fields = (
            "max_iterations",
            "max_total_tool_calls",
            "max_tool_calls_per_iteration",
            "max_consecutive_parse_errors",
            "max_model_retries",
            "max_repeated_failed_tool_call",
            "max_same_tool_call",
        )
        for field_name in positive_integer_fields:
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds must be greater than zero")
        if self.model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds must be greater than zero")


def tool_call_signature(tool_call: ToolCall) -> str:
    """Return a stable key for detecting repeated model tool calls."""
    return json.dumps(
        {
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
