"""Provider- and transport-neutral observability events."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AgentEventType(str, Enum):
    SESSION_STARTED = "session_started"
    USER_MESSAGE = "user_message"
    ITERATION_STARTED = "iteration_started"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    ASSISTANT_MESSAGE = "assistant_message"
    PLAN = "plan"
    REFLECTION = "reflection"
    CONTEXT_TRUNCATED = "context_truncated"
    CONTEXT_COMPACTED = "context_compacted"
    AGENT_FINISHED = "agent_finished"
    AGENT_ERROR = "agent_error"


class AgentEvent(BaseModel):
    """One observable state transition in an Agent session."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    type: AgentEventType
    session_id: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    iteration: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
