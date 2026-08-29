"""HTTP request and response models for the optional API layer."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from coding_agent.events import AgentEvent


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: Path


class DirectorySelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: Path


class WorkspaceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    truncated: bool = False


class WorkspaceEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    workspace_root: Path
    status: str = "idle"
    run_id: str | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    auto_approve: bool = False


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    status: str


class HistorySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    workspace_root: Path | None = None
    task: str | None = None
    title: str = "New conversation"
    title_status: str = "pending"
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    iterations: int = 0
    tool_calls: int = 0
    event_count: int = 0
    turn_count: int = 1
    error: str | None = None


class HistoryRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: HistorySummaryResponse
    events: list[AgentEvent]


class CancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    approve_current_turn: bool = False


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    status: str


class RevertChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str
    status: str
    diff: str
