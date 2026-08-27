from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.agent import Agent, AgentLimits, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.events import AgentEventType
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ToolExecutor, ToolRegistry
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS


def make_agent(
    tmp_path: Path,
    responses: list[ModelResponse | BaseException],
    *,
    limits: AgentLimits | None = None,
    events: list | None = None,
) -> tuple[Agent, MockModelProvider, list]:
    event_log = events if events is not None else []
    provider = MockModelProvider(responses)
    executor = ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS), event_handler=event_log.append)
    agent = Agent(
        provider,
        executor,
        ContextManager("You are a coding agent."),
        Session(session_id="session-1", workspace_root=tmp_path),
        limits=limits,
        event_handler=event_log.append,
    )
    return agent, provider, event_log


def run(agent: Agent, task: str = "inspect the project"):
    return asyncio.run(agent.run(task))


def test_agent_success_path_executes_tool_and_returns_final_answer(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    agent, provider, events = make_agent(
        tmp_path,
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "main.py"},
                    )
                ]
            ),
            ModelResponse(content="I inspected main.py successfully."),
        ],
    )

    result = run(agent)

    assert result.status == SessionStatus.COMPLETED
    assert result.final_answer == "I inspected main.py successfully."
    assert result.iterations == 2
    assert result.tool_calls == 1
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role == "tool"
    assert [event.type for event in events].count(AgentEventType.TOOL_STARTED) == 1
    assert AgentEventType.AGENT_FINISHED in [event.type for event in events]


def test_agent_returns_tool_failure_to_model_and_can_finish(tmp_path: Path) -> None:
    agent, provider, _ = make_agent(
        tmp_path,
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "missing.py"},
                    )
                ]
            ),
            ModelResponse(content="The file was not present, so no change was needed."),
        ],
    )

    result = run(agent)

    assert result.status == SessionStatus.COMPLETED
    assert provider.requests[1].messages[-1].role == "tool"
    assert '"success": false' in (provider.requests[1].messages[-1].content or "")


def test_agent_stops_after_repeated_failed_tool_call(tmp_path: Path) -> None:
    repeated_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "missing.py"},
    )
    agent, provider, _ = make_agent(
        tmp_path,
        [ModelResponse(tool_calls=[repeated_call]), ModelResponse(tool_calls=[repeated_call])],
        limits=AgentLimits(max_repeated_failed_tool_call=2),
    )

    result = run(agent)

    assert result.status == SessionStatus.FAILED
    assert result.error == "maximum repeated failed tool call exceeded"
    assert len(provider.requests) == 2


def test_agent_stops_at_max_iterations(tmp_path: Path) -> None:
    call = ToolCall(id="call-1", name="list_files", arguments={})
    agent, _, _ = make_agent(
        tmp_path,
        [ModelResponse(tool_calls=[call]) for _ in range(3)],
        limits=AgentLimits(max_iterations=2),
    )

    result = run(agent)

    assert result.status == SessionStatus.FAILED
    assert result.error == "maximum iterations exceeded"
    assert result.iterations == 2


def test_agent_stops_after_repeated_empty_model_responses(tmp_path: Path) -> None:
    agent, provider, _ = make_agent(
        tmp_path,
        [ModelResponse(), ModelResponse()],
        limits=AgentLimits(max_consecutive_parse_errors=2),
    )

    result = run(agent)

    assert result.status == SessionStatus.FAILED
    assert result.error == "model returned empty content and no tool calls"
    assert len(provider.requests) == 2


def test_agent_can_be_cancelled_before_next_model_call(tmp_path: Path) -> None:
    agent, provider, _ = make_agent(tmp_path, [ModelResponse(content="should not run")])
    agent.cancel()

    result = run(agent)

    assert result.status == SessionStatus.CANCELLED
    assert result.error == "agent run cancelled"
    assert len(provider.requests) == 0
