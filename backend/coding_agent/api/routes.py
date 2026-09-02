"""HTTP routes for the optional FastAPI transport layer."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse

from coding_agent.events import AgentEvent
from coding_agent.trace import summarize_events

from .app import ApiState, RunRecord
from .schemas import (
    CancelResponse,
    ApprovalDecisionRequest,
    ApprovalResponse,
    CreateSessionRequest,
    DirectorySelectionResponse,
    HistoryRecordResponse,
    HistorySummaryResponse,
    RenameSessionRequest,
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
    pending = [root]
    while pending and len(entries) < 1000:
        current = pending.pop(0)
        try:
            with os.scandir(current) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for child in children:
            if len(entries) >= 1000:
                break
            try:
                relative = Path(child.path).relative_to(root)
                if ".git" in {part.casefold() for part in relative.parts}:
                    continue
                if child.is_symlink():
                    continue
                is_directory = child.is_dir(follow_symlinks=False)
                if not is_directory and not child.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            entries.append(
                WorkspaceEntryResponse(
                    path=relative.as_posix(),
                    kind="directory" if is_directory else "file",
                )
            )
            if is_directory:
                pending.append(Path(child.path))
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


def _history_summary(
    run_id: str,
    events: list[AgentEvent],
    *,
    turn_count: int | None = None,
    title: str | None = None,
) -> HistorySummaryResponse:
    summary = summarize_events(events)
    started_at = events[0].timestamp.isoformat() if events else None
    finished_at = events[-1].timestamp.isoformat() if events else None
    resolved_title = title or (summary.tasks[0] if summary.tasks else "New conversation")
    return HistorySummaryResponse(
        run_id=run_id,
        session_id=summary.session_id or "unknown",
        workspace_root=summary.workspace_root,
        task=summary.tasks[-1] if summary.tasks else None,
        title=resolved_title,
        status=summary.status or "running",
        started_at=started_at,
        finished_at=finished_at if summary.status else None,
        duration_seconds=summary.run_duration_seconds,
        iterations=summary.iterations,
        tool_calls=summary.tool_calls,
        event_count=summary.event_count,
        turn_count=turn_count if turn_count is not None else max(1, len(summary.tasks)),
        error=summary.error,
    )


def _read_session_history(state: ApiState, session_id: str) -> tuple[str, list[AgentEvent]]:
    """Read all recorded turns for one session in chronological order."""
    run_ids = state.history_store.list_run_ids_for_session(session_id)
    events = state.history_store.read_session(session_id)
    if not run_ids or not events:
        return "", []
    events.sort(key=lambda event: event.timestamp)
    return run_ids[0], events


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

    @app.get("/history", response_model=list[HistorySummaryResponse])
    async def list_history(limit: int = 50) -> list[HistorySummaryResponse]:
        """List persisted conversations without restoring or executing Agent state."""
        bounded_limit = max(1, min(limit, 200))
        grouped: dict[str, list[tuple[str, list[AgentEvent]]]] = {}
        for run_id in state.history_store.list_run_ids():
            try:
                events = state.history_store.read(run_id)
                if events and events[0].session_id:
                    grouped.setdefault(events[0].session_id, []).append((run_id, events))
            except ValueError:
                # A partially written or manually damaged log is not allowed to
                # make the rest of the local history unavailable.
                continue
        history: list[HistorySummaryResponse] = []
        for session_runs in grouped.values():
            session_events = [event for _, events in session_runs for event in events]
            session_events.sort(key=lambda event: event.timestamp)
            history.append(
                _history_summary(
                    session_runs[0][0],
                    session_events,
                    turn_count=sum(event.type.value == "user_message" for event in session_events),
                    title=state.history_store.get_session_title(session_events[0].session_id),
                )
            )
        return history[:bounded_limit]

    @app.get("/history/sessions/{session_id}", response_model=HistoryRecordResponse)
    async def read_session_history(session_id: str) -> HistoryRecordResponse:
        """Return a complete conversation for read-only replay."""
        latest_run_id, events = _read_session_history(state, session_id)
        if not events:
            raise HTTPException(status_code=404, detail="session history is empty")
        return HistoryRecordResponse(
            summary=_history_summary(
                latest_run_id,
                events,
                turn_count=sum(event.type.value == "user_message" for event in events),
                title=state.history_store.get_session_title(session_id),
            ),
            events=events,
        )

    @app.get("/history/{run_id}", response_model=HistoryRecordResponse)
    async def read_history(run_id: str) -> HistoryRecordResponse:
        """Return a recorded run for read-only replay."""
        try:
            events = state.history_store.read(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not events:
            raise HTTPException(status_code=404, detail="run history is empty")
        return HistoryRecordResponse(
            summary=_history_summary(
                run_id,
                events,
                title=state.history_store.get_session_title(events[0].session_id),
            ),
            events=events,
        )

    @app.post("/history/{run_id}/continue", response_model=SessionResponse)
    async def continue_history(run_id: str) -> SessionResponse:
        """Restore the historical context for a new user turn."""
        try:
            session = state.resume_history(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SessionResponse(
            session_id=session.session_id,
            workspace_root=session.workspace.root,
        )

    @app.post("/history/sessions/{session_id}/activate", response_model=SessionResponse)
    async def activate_history_session(session_id: str) -> SessionResponse:
        """Switch to a conversation and keep it writable for future turns."""
        try:
            session, live_run = state.activate_history(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SessionResponse(
            session_id=session.session_id,
            workspace_root=session.workspace.root,
            status="running" if live_run is not None else "idle",
            run_id=live_run.run_id if live_run is not None else None,
        )

    @app.delete("/history/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_history_session(session_id: str) -> None:
        """Delete one conversation and its persisted history."""
        session = state.sessions.get(session_id)
        if session is not None and session.current_run is not None and not session.current_run.finished:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot delete a running session")
        if not state.history_store.delete_session(session_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session history not found")
        state.sessions.pop(session_id, None)

    @app.patch("/history/sessions/{session_id}", response_model=HistorySummaryResponse)
    async def rename_history_session(session_id: str, request: RenameSessionRequest) -> HistorySummaryResponse:
        session = state.sessions.get(session_id)
        if session is not None and session.current_run is not None and not session.current_run.finished:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot rename a running session")
        title = state.history_store.rename_session(session_id, request.title)
        if title is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session history not found")
        run_ids = state.history_store.list_run_ids_for_session(session_id)
        events = state.history_store.read_session(session_id)
        if not run_ids or not events:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session history not found")
        return _history_summary(run_ids[0], events, turn_count=sum(event.type.value == "user_message" for event in events), title=title)

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
