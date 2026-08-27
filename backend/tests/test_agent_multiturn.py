from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from coding_agent.agent import Agent, AgentLimits, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.models import ModelRequest, ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ToolExecutor, ToolRegistry
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS


def make_agent(
    tmp_path: Path,
    responses: list[ModelResponse | BaseException],
    *,
    limits: AgentLimits | None = None,
) -> tuple[Agent, MockModelProvider]:
    provider = MockModelProvider(responses)
    agent = Agent(
        provider,
        ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS)),
        ContextManager("You are a coding agent."),
        Session(session_id="multi-turn-session", workspace_root=tmp_path),
        limits=limits,
    )
    return agent, provider


def test_sequential_turns_reuse_session_and_context(tmp_path: Path) -> None:
    agent, provider = make_agent(
        tmp_path,
        [
            ModelResponse(content="The first answer."),
            ModelResponse(content="The second answer remembers the first."),
        ],
    )

    async def scenario():
        first = await agent.run_turn("Remember this instruction.")
        second = await agent.run_turn("What did I ask you to remember?")
        return first, second

    first, second = asyncio.run(scenario())

    assert first.status == SessionStatus.COMPLETED
    assert second.status == SessionStatus.COMPLETED
    assert second.final_answer == "The second answer remembers the first."
    assert agent.session.status == SessionStatus.IDLE
    assert agent.session.session_id == "multi-turn-session"
    assert [message.content for message in provider.requests[1].messages] == [
        "You are a coding agent.",
        "Remember this instruction.",
        "The first answer.",
        "What did I ask you to remember?",
    ]


def test_run_remains_backward_compatible_with_one_shot_call(tmp_path: Path) -> None:
    agent, provider = make_agent(tmp_path, [ModelResponse(content="one-shot answer")])

    result = asyncio.run(agent.run("run one task"))

    assert result.status == SessionStatus.COMPLETED
    assert result.final_answer == "one-shot answer"
    assert len(provider.requests) == 1


def test_tool_result_from_first_turn_remains_in_second_request(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep this note", encoding="utf-8")
    agent, provider = make_agent(
        tmp_path,
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="read_file",
                        arguments={"path": "notes.txt"},
                    )
                ]
            ),
            ModelResponse(content="I read the note."),
            ModelResponse(content="Yes, the note said keep this note."),
        ],
    )

    async def scenario():
        first = await agent.run_turn("Read notes.txt.")
        second = await agent.run_turn("What did the note say?")
        return first, second

    first, second = asyncio.run(scenario())

    assert first.status == SessionStatus.COMPLETED
    assert second.status == SessionStatus.COMPLETED
    assert first.tool_calls == 1
    assert second.tool_calls == 0
    second_messages = provider.requests[2].messages
    assert any(
        message.role == "tool" and "keep this note" in (message.content or "")
        for message in second_messages
    )


def test_per_turn_repeated_failure_counter_is_reset(tmp_path: Path) -> None:
    failing_call = ToolCall(
        id="missing-1",
        name="read_file",
        arguments={"path": "missing.txt"},
    )
    agent, _ = make_agent(
        tmp_path,
        [
            ModelResponse(tool_calls=[failing_call]),
            ModelResponse(content="The first missing file was handled."),
            ModelResponse(tool_calls=[failing_call]),
            ModelResponse(content="The second missing file was handled."),
        ],
        limits=AgentLimits(max_repeated_failed_tool_call=2),
    )

    async def scenario():
        return await agent.run_turn("Check the file once."), await agent.run_turn(
            "Check the file again."
        )

    first, second = asyncio.run(scenario())

    assert first.status == SessionStatus.COMPLETED
    assert second.status == SessionStatus.COMPLETED
    assert first.tool_calls == 1
    assert second.tool_calls == 1


class BlockingProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        self.started.set()
        await self.release.wait()
        return self.responses.popleft()


def test_overlapping_turns_are_rejected_without_context_corruption(tmp_path: Path) -> None:
    provider = BlockingProvider(
        [ModelResponse(content="first"), ModelResponse(content="second")]
    )
    agent = Agent(
        provider,
        ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS)),
        ContextManager("You are a coding agent."),
        Session(session_id="overlap-session", workspace_root=tmp_path),
    )

    async def scenario():
        first_task = asyncio.create_task(agent.run_turn("first turn"))
        await provider.started.wait()
        second = await agent.run_turn("second turn")
        provider.release.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(scenario())

    assert first.status == SessionStatus.COMPLETED
    assert second.status == SessionStatus.FAILED
    assert second.error == "agent is already running a turn"
    assert len(provider.requests) == 1
    assert [message.content for message in provider.requests[0].messages][-1] == "first turn"


def test_cancelled_turn_does_not_poison_the_next_turn(tmp_path: Path) -> None:
    provider = BlockingProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="cancel-1",
                        name="list_files",
                        arguments={},
                    )
                ]
            ),
            ModelResponse(content="the next turn works"),
        ]
    )
    agent = Agent(
        provider,
        ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS)),
        ContextManager("You are a coding agent."),
        Session(session_id="cancel-session", workspace_root=tmp_path),
    )

    async def scenario():
        first_task = asyncio.create_task(agent.run_turn("cancel this"))
        await provider.started.wait()
        agent.cancel()
        provider.release.set()
        first = await first_task
        second = await agent.run_turn("continue")
        return first, second

    first, second = asyncio.run(scenario())

    assert first.status == SessionStatus.CANCELLED
    assert second.status == SessionStatus.COMPLETED
    assert second.final_answer == "the next turn works"
