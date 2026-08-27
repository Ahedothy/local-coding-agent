from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from coding_agent.events import AgentEventType
from coding_agent.models import ToolCall
from coding_agent.tools import Tool, ToolContext, ToolExecutor, ToolRegistry, ToolResult
from coding_agent.workspace import Workspace


class EchoArguments(BaseModel):
    text: str = Field(min_length=1)


class EchoTool(Tool):
    name = "echo"
    description = "Return the supplied text."
    parameters_model = EchoArguments

    async def execute(self, context: ToolContext, arguments: EchoArguments) -> ToolResult:
        return ToolResult(
            tool_call_id="tool-created-id",
            tool_name="tool-created-name",
            success=True,
            output={"text": arguments.text, "session_id": context.session_id},
        )


class RaisingTool(Tool):
    name = "raise_error"
    description = "Raise an exception for executor testing."
    parameters_model = EchoArguments

    async def execute(self, context: ToolContext, arguments: EchoArguments) -> ToolResult:
        raise RuntimeError("expected failure")


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def test_registry_rejects_duplicate_names_and_exports_schema() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())

    assert registry.get("echo") is not None
    assert registry.get("missing") is None
    assert registry.model_schemas() == [
        {
            "name": "echo",
            "description": "Return the supplied text.",
            "parameters": EchoArguments.model_json_schema(),
        }
    ]


def test_executor_validates_and_passes_context_to_tool(tmp_path: Path) -> None:
    registry = ToolRegistry([EchoTool()])
    executor = ToolExecutor(registry)

    result = asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="echo", arguments={"text": "hello"}),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.tool_call_id == "call-1"
    assert result.tool_name == "echo"
    assert result.output == {"text": "hello", "session_id": "session-1"}


def test_executor_returns_failure_for_unknown_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry())

    result = asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="missing", arguments={}),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert result.error == "unknown tool: missing"


def test_executor_returns_failure_for_invalid_arguments(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry([EchoTool()]))

    result = asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="echo", arguments={"text": ""}),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert result.tool_name == "echo"
    assert result.error is not None
    assert "invalid arguments" in result.error


def test_executor_converts_tool_exception_to_failure(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry([RaisingTool()]))

    result = asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="raise_error", arguments={"text": "hello"}),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert result.error == "tool execution failed: expected failure"


def test_executor_emits_tool_lifecycle_events(tmp_path: Path) -> None:
    events = []
    executor = ToolExecutor(ToolRegistry([EchoTool()]), event_handler=events.append)

    asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="echo", arguments={"text": "hello"}),
            make_context(tmp_path),
        )
    )

    assert [event.type for event in events] == [
        AgentEventType.TOOL_STARTED,
        AgentEventType.TOOL_FINISHED,
    ]
