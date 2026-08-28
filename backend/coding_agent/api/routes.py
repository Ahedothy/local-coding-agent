"""HTTP routes for the optional FastAPI transport layer."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse

from coding_agent.events import AgentEvent

from .app import ApiState, RunRecord
from .schemas import (
    CancelResponse,
    ApprovalDecisionRequest,
    ApprovalResponse,
    CreateSessionRequest,
    DirectorySelectionResponse,
    RunRequest,
    RunResponse,
    RevertChangeResponse,
    SessionResponse,
    WorkspaceEntryResponse,
    WorkspaceFileResponse,
)
from coding_agent.workspace import Workspace


def _scan_workspace_entries(root: Path) -> list[WorkspaceEntryResponse]:
    entries: list[WorkspaceEntryResponse] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in {part.casefold() for part in relative.parts}:
            continue
        entries.append(
            WorkspaceEntryResponse(
                path=relative.as_posix(),
                kind="directory" if path.is_dir() else "file",
            )
        )
    return sorted(
        entries,
        key=lambda entry: (entry.path.count("/"), entry.kind != "directory", entry.path.casefold()),
    )[:1000]


def _sse_message(event: AgentEvent) -> str:
    return (
        f"id: {event.event_id}\n"
        f"event: {event.type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


async def _event_stream(run: RunRecord):
    cursor = 0
    while True:
        while cursor < len(run.events):
            event = run.events[cursor]
            cursor += 1
            yield _sse_message(event)

        if run.finished:
            return
        await run.wakeup.wait()
        run.wakeup.clear()


def register_routes(app: FastAPI, state: ApiState) -> None:
    """Register transport routes without adding orchestration to the API layer."""

    @app.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
    async def create_session(request: CreateSessionRequest) -> SessionResponse:
        try:
            workspace = Workspace(request.workspace_root)
            session = state.create_session(workspace)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SessionResponse(
            session_id=session.session_id,
            workspace_root=session.workspace.root,
        )

    @app.get("/workspaces/select", response_model=DirectorySelectionResponse)
    async def select_workspace() -> DirectorySelectionResponse:
        """Open a native folder picker on the same machine as the API server."""
        def choose() -> str:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="Choose a local coding workspace")
            root.destroy()
            return selected

        selected = await asyncio.to_thread(choose)
        if not selected:
            raise HTTPException(status_code=400, detail="no workspace selected")
        try:
            workspace = Workspace(selected)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return DirectorySelectionResponse(workspace_root=workspace.root)

    @app.get("/sessions/{session_id}/files", response_model=list[WorkspaceEntryResponse])
    async def list_session_files(session_id: str) -> list[WorkspaceEntryResponse]:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return await asyncio.to_thread(_scan_workspace_entries, session.workspace.root)

    @app.get("/sessions/{session_id}/files/{file_path:path}", response_model=WorkspaceFileResponse)
    async def read_session_file(session_id: str, file_path: str) -> WorkspaceFileResponse:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            path = session.workspace.ensure_readable_file(file_path)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="file is not a supported UTF-8 text file",
                ) from exc
            if "\x00" in content[:8_192]:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="binary file preview is not supported",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        truncated = len(content) > 100_000
        return WorkspaceFileResponse(path=file_path, content=content[:100_000], truncated=truncated)

    @app.post(
        "/sessions/{session_id}/runs",
        response_model=RunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(session_id: str, request: RunRequest) -> RunResponse:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        run = await state.start_run(session, request.task, request.auto_approve)
        return RunResponse(
            run_id=run.run_id,
            session_id=run.session_id,
            status="running",
        )

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return StreamingResponse(
            _event_stream(run),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/runs/{run_id}/cancel", response_model=CancelResponse)
    async def cancel_run(run_id: str) -> CancelResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not run.finished:
            run.agent.cancel()
            return CancelResponse(run_id=run_id, status="cancellation_requested")
        return CancelResponse(run_id=run_id, status="already_finished")

    @app.post(
        "/sessions/{session_id}/approvals/{approval_id}",
        response_model=ApprovalResponse,
    )
    async def decide_approval(
        session_id: str,
        approval_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalResponse:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        request_record = session.approval_gate.decide(
            approval_id,
            request.approved,
            request.approve_current_turn,
        )
        if request_record is None or request_record.session_id != session_id:
            raise HTTPException(status_code=404, detail="approval request not found")
        return ApprovalResponse(
            approval_id=approval_id,
            status="approved" if request.approved else "rejected",
        )

    @app.post(
        "/sessions/{session_id}/changes/{change_id}/revert",
        response_model=RevertChangeResponse,
    )
    async def revert_change(session_id: str, change_id: str) -> RevertChangeResponse:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        change = session.change_store.get(change_id)
        if change is None:
            raise HTTPException(status_code=404, detail="change not found")
        was_reverted = change.reverted
        try:
            session.change_store.revert(change_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RevertChangeResponse(
            change_id=change_id,
            status="already_reverted" if was_reverted else "reverted",
            diff=change.diff,
        )
