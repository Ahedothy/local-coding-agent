from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from coding_agent.agent import Agent, Session
from coding_agent.api import create_app
from coding_agent.context import ContextManager
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ToolExecutor, ToolRegistry
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.workspace import Workspace


def test_windows_api_configures_subprocess_capable_event_loop_policy() -> None:
    if sys.platform != "win32":
        return

    policy_type = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_type is not None:
        assert isinstance(asyncio.get_event_loop_policy(), policy_type)


def make_factory(responses: list[ModelResponse]):
    def factory(workspace: Workspace, event_handler) -> Agent:
        provider = MockModelProvider(responses)
        session = Session(workspace_root=workspace.root)
        return Agent(
            provider,
            ToolExecutor(ToolRegistry([]), event_handler=event_handler),
            ContextManager("You are a test agent."),
            session,
            event_handler=event_handler,
        )

    return factory


def test_api_creates_session_starts_run_and_streams_sse_events(tmp_path: Path) -> None:
    app = create_app(make_factory([ModelResponse(content="completed from API")]))

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_response = await client.post(
                "/sessions",
                json={"workspace_root": str(tmp_path)},
            )
            assert session_response.status_code == 201
            session_id = session_response.json()["session_id"]

            run_response = await client.post(
                f"/sessions/{session_id}/runs",
                json={"task": "say hello"},
            )
            assert run_response.status_code == 202
            run_id = run_response.json()["run_id"]

            return await client.get(f"/runs/{run_id}/events")

    event_response = asyncio.run(scenario())
    assert event_response.status_code == 200
    assert event_response.headers["content-type"].startswith("text/event-stream")
    assert "event: session_started" in event_response.text
    assert "event: model_request" in event_response.text
    assert "event: agent_finished" in event_response.text
    data_lines = [
        line.removeprefix("data: ")
        for line in event_response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert data_lines
    assert json.loads(data_lines[-1])["type"] == "agent_finished"


def test_api_rejects_overlapping_runs(tmp_path: Path) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request):
            self.started.set()
            await self.release.wait()
            return ModelResponse(content="done")

    provider = BlockingProvider()

    def factory(workspace: Workspace, event_handler) -> Agent:
        from coding_agent.agent import Session

        session = Session(workspace_root=workspace.root)
        return Agent(
            provider,
            ToolExecutor(ToolRegistry([]), event_handler=event_handler),
            ContextManager("Test"),
            session,
            event_handler=event_handler,
        )

    app = create_app(factory)
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (
                await client.post(
                    "/sessions", json={"workspace_root": str(tmp_path)}
                )
            ).json()["session_id"]
            first = await client.post(
                f"/sessions/{session_id}/runs", json={"task": "first"}
            )
            second = await client.post(
                f"/sessions/{session_id}/runs", json={"task": "second"}
            )
            provider.release.set()
            await client.get(f"/runs/{first.json()['run_id']}/events")
            return first, second

    first, second = asyncio.run(scenario())

    assert first.status_code == 202
    assert second.status_code == 409


def test_api_cancel_requests_agent_cancellation(tmp_path: Path) -> None:
    app = create_app(make_factory([ModelResponse(content="done")]))

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (
                await client.post(
                    "/sessions", json={"workspace_root": str(tmp_path)}
                )
            ).json()["session_id"]
            run_id = (
                await client.post(
                    f"/sessions/{session_id}/runs", json={"task": "cancel me"}
                )
            ).json()["run_id"]
            return await client.post(f"/runs/{run_id}/cancel")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json()["status"] in {
        "cancellation_requested",
        "already_finished",
    }


def test_api_file_preview_distinguishes_text_and_binary_files(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    binary_file = tmp_path / "image.bin"
    text_file.write_text("hello\nworld\n", encoding="utf-8")
    binary_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
    app = create_app(make_factory([]))

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (
                await client.post(
                    "/sessions", json={"workspace_root": str(tmp_path)}
                )
            ).json()["session_id"]
            text_response = await client.get(f"/sessions/{session_id}/files/notes.txt")
            binary_response = await client.get(f"/sessions/{session_id}/files/image.bin")
            return text_response, binary_response

    text_response, binary_response = asyncio.run(scenario())

    assert text_response.status_code == 200
    assert text_response.json()["content"] == "hello\nworld\n"
    assert binary_response.status_code == 415
    assert "supported" in binary_response.json()["detail"]


def test_api_approval_endpoint_releases_a_local_edit(tmp_path: Path) -> None:
    def factory(workspace: Workspace, event_handler) -> Agent:
        session = Session(workspace_root=workspace.root)
        return Agent(
            MockModelProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="write-1",
                                name="write_file",
                                arguments={"path": "hello.txt", "content": "hello\n"},
                            )
                        ]
                    ),
                    ModelResponse(content="Created hello.txt."),
                ]
            ),
            ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS), event_handler=event_handler),
            ContextManager("You are a test agent."),
            session,
            event_handler=event_handler,
        )

    app = create_app(factory)

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (
                await client.post("/sessions", json={"workspace_root": str(tmp_path)})
            ).json()["session_id"]
            run_id = (
                await client.post(
                    f"/sessions/{session_id}/runs",
                    json={"task": "create hello.txt"},
                )
            ).json()["run_id"]
            state = app.state.agent_state
            approval_id = None
            for _ in range(100):
                pending = state.sessions[session_id].approval_gate._pending
                if pending:
                    approval_id = next(iter(pending))
                    break
                await asyncio.sleep(0.01)
            assert approval_id is not None
            decision = await client.post(
                f"/sessions/{session_id}/approvals/{approval_id}",
                json={"approved": True, "approve_current_turn": True},
            )
            events = await client.get(f"/runs/{run_id}/events")
            event_payloads = [
                json.loads(line.removeprefix("data: "))
                for line in events.text.splitlines()
                if line.startswith("data: ")
            ]
            finished_tool = next(
                payload
                for payload in event_payloads
                if payload["type"] == "tool_finished"
            )
            change_id = finished_tool["payload"]["metadata"]["change_id"]
            revert = await client.post(
                f"/sessions/{session_id}/changes/{change_id}/revert"
            )
            return decision, events, revert

    decision, events, revert = asyncio.run(scenario())

    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"
    assert events.status_code == 200
    assert "event: approval_requested" in events.text
    assert "event: approval_resolved" in events.text
    assert "event: agent_finished" in events.text
    assert revert.status_code == 200
    assert revert.json()["status"] == "reverted"
    assert not (tmp_path / "hello.txt").exists()
