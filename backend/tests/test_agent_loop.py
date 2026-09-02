from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from coding_agent.agent import Agent, AgentLimits, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.events import AgentEventType
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.models import ModelProviderError
from coding_agent.tools import Tool, ToolContext, ToolExecutor, ToolRegistry, ToolResult
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS


class VerifyArguments(BaseModel):
    pass


class VerifyTool(Tool):
    name = "verify_done"
    description = "Record successful verification evidence."
    parameters_model = VerifyArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: VerifyArguments,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "command": ["verify_done"],
                "returncode": 0,
                "stdout": "verification passed",
                "verification": {
                    "kind": "build",
                    "criteria": ["workspace has been verified"],
                },
            },
        )


def make_agent(
    tmp_path: Path,
    responses: list[ModelResponse | BaseException],
    *,
    limits: AgentLimits | None = None,
    events: list | None = None,
    extra_tools: list[Tool] | None = None,
) -> tuple[Agent, MockModelProvider, list]:
    event_log = events if events is not None else []
    provider = MockModelProvider(responses)
    executor = ToolExecutor(
        ToolRegistry([*FILESYSTEM_TOOLS, *(extra_tools or [])]),
        event_handler=event_log.append,
    )
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
                ],
                usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                raw_metadata={"model": "mock-model"},
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
    model_response = next(
        event for event in events if event.type is AgentEventType.MODEL_RESPONSE
    )
    assert model_response.payload["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    assert model_response.payload["model_metadata"] == {"model": "mock-model"}
    assert isinstance(model_response.payload["duration_seconds"], float)
    finished = next(
        event for event in events if event.type is AgentEventType.AGENT_FINISHED
    )
    assert isinstance(finished.payload["duration_seconds"], float)


def test_agent_retries_transient_model_provider_error(tmp_path: Path) -> None:
    agent, provider, events = make_agent(
        tmp_path,
        [
            ModelProviderError("temporary outage", retryable=True, reason="temporary_server_error_503"),
            ModelResponse(content="Recovered after a transient model error."),
        ],
    )

    result = run(agent)

    assert result.status == SessionStatus.COMPLETED
    assert len(provider.requests) == 2
    retry = next(event for event in events if event.type is AgentEventType.MODEL_RETRY_SCHEDULED)
    assert retry.payload["attempt"] == 2
    assert retry.payload["reason"] == "temporary_server_error_503"
    assert retry.payload["delay_seconds"] == 0.5


def test_agent_does_not_retry_non_retryable_provider_error(tmp_path: Path) -> None:
    agent, provider, events = make_agent(
        tmp_path,
        [ModelProviderError("invalid api key", retryable=False, reason="authentication")],
    )

    result = run(agent)

    assert result.status == SessionStatus.FAILED
    assert len(provider.requests) == 1
    assert not any(event.type is AgentEventType.MODEL_RETRY_SCHEDULED for event in events)


def test_agent_emits_plan_then_reflection_for_nontrivial_tool_turn(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    agent, provider, events = make_agent(
        tmp_path,
        [
            ModelResponse(
                content="Plan:\n- inspect the source\n- run a focused check",
                tool_calls=[
                    ToolCall(
                        id="read-source",
                        name="read_file",
                        arguments={"path": "main.py"},
                    )
                ],
            ),
            ModelResponse(
                content="The source is minimal; I will verify the workspace next.",
                tool_calls=[
                    ToolCall(
                        id="list-workspace",
                        name="list_files",
                        arguments={},
                    )
                ],
            ),
            ModelResponse(content="The inspection is complete."),
        ],
    )

    result = run(agent, "Inspect the source and verify the workspace.")

    assert result.status == SessionStatus.COMPLETED
    planning_events = [
        event
        for event in events
        if event.type in {AgentEventType.PLAN, AgentEventType.REFLECTION}
    ]
    assert [event.type for event in planning_events] == [
        AgentEventType.PLAN,
        AgentEventType.REFLECTION,
    ]
    assert planning_events[0].payload["content"].startswith("Plan:")
    assert planning_events[1].payload["content"].startswith("The source is minimal")
    assert provider.requests[1].messages[-2].content.startswith("Plan:")


def test_agent_does_not_emit_plan_for_simple_final_response(tmp_path: Path) -> None:
    agent, _, events = make_agent(
        tmp_path,
        [ModelResponse(content="A direct answer is enough.")],
    )

    result = run(agent, "Say hello.")

    assert result.status == SessionStatus.COMPLETED
    assert not any(
        event.type in {AgentEventType.PLAN, AgentEventType.REFLECTION}
        for event in events
    )


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


def test_agent_generates_final_answer_when_verified_completion_hits_iteration_limit(
    tmp_path: Path,
) -> None:
    verify_call = ToolCall(id="verify-1", name="verify_done", arguments={})
    agent, provider, events = make_agent(
        tmp_path,
        [
            ModelResponse(tool_calls=[verify_call]),
            ModelResponse(tool_calls=[verify_call]),
            ModelResponse(content="已完成计算器封装，并通过构建验证。"),
        ],
        limits=AgentLimits(max_iterations=2),
        extra_tools=[VerifyTool()],
    )

    result = run(agent)

    assert result.status == SessionStatus.COMPLETED
    assert result.final_answer == "已完成计算器封装，并通过构建验证。"
    assert len(provider.requests) == 3
    assert provider.requests[-1].tools == []
    assert provider.requests[-1].messages[-1].role == "user"
    assert "请生成面向用户的最终回复" in (
        provider.requests[-1].messages[-1].content or ""
    )
    final_message = [
        event
        for event in events
        if event.type is AgentEventType.ASSISTANT_MESSAGE
        and event.payload["tool_call_count"] == 0
    ][-1]
    assert final_message.payload["content"] == "已完成计算器封装，并通过构建验证。"
    assert final_message.payload["verified_final_answer"] is True


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
