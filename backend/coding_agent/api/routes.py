"""HTTP routes for the optional FastAPI transport layer."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse

from coding_agent.events import AgentEvent

from .app import ApiState, RunRecord
from .schemas import (
    CancelResponse,
    CreateSessionRequest,
    RunRequest,
    RunResponse,
    SessionResponse,
)
from coding_agent.workspace import Workspace


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

    @app.post(
        "/sessions/{session_id}/runs",
        response_model=RunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(session_id: str, request: RunRequest) -> RunResponse:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        run = await state.start_run(session, request.task)
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
