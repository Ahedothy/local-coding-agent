"""HTTP request and response models for the optional API layer."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: Path


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    workspace_root: Path


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    status: str


class CancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
