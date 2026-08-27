from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.agent import Session, SessionStatus
from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ModelMessage, ModelRequest, ModelResponse, ToolCall
from coding_agent.tools import ToolContext, ToolResult
from coding_agent.workspace import Workspace


def test_model_request_and_response_are_provider_neutral() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "main.py"})
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="Inspect the project")],
        tools=[{"name": "read_file", "parameters": {"type": "object"}}],
    )
    response = ModelResponse(content="I will inspect it.", tool_calls=[call])

    assert request.messages[0].role == "user"
    assert response.tool_calls[0].arguments["path"] == "main.py"
    assert request.model_dump()["messages"][0]["role"] == "user"


def test_message_role_fields_are_validated() -> None:
    with pytest.raises(ValidationError):
        ModelMessage(role="tool", content="result")

    with pytest.raises(ValidationError):
        ModelMessage(role="user", tool_calls=[ToolCall(id="1", name="x")])


def test_tool_result_requires_consistent_success_state() -> None:
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output="hello",
    )
    assert result.success is True

    with pytest.raises(ValidationError):
        ToolResult(
            tool_call_id="call-1",
            tool_name="read_file",
            success=False,
        )


def test_tool_context_keeps_workspace_as_a_local_runtime_dependency(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    context = ToolContext(session_id="session-1", workspace=workspace)

    assert context.workspace.root == tmp_path.resolve()
    assert context.cancellation_token is None


def test_agent_event_and_session_are_serializable() -> None:
    event = AgentEvent(
        type=AgentEventType.SESSION_STARTED,
        session_id="session-1",
        payload={"workspace": "demo"},
    )
    session = Session(session_id="session-1", workspace_root=Path("demo"))

    assert event.type == AgentEventType.SESSION_STARTED
    assert event.model_dump(mode="json")["type"] == "session_started"
    assert session.status == SessionStatus.CREATED
    assert session.model_dump(mode="json")["workspace_root"] == "demo"
