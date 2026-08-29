from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.agent import Agent, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.events import AgentEventType
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ApprovalGate, ChangeStore, ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def test_edit_waits_for_approval_and_can_be_reverted(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    gate = ApprovalGate()
    changes = ChangeStore(Workspace(tmp_path))
    events = []
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        event_handler=events.append,
        approval_gate=gate,
        change_store=changes,
    )

    async def scenario():
        operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="edit-1",
                    name="write_file",
                    arguments={"path": "main.py", "content": "value = 2\n"},
                ),
                make_context(tmp_path),
            )
        )
        await asyncio.sleep(0)
        approval_event = next(event for event in events if event.type is AgentEventType.APPROVAL_REQUESTED)
        approval_id = approval_event.payload["approval_id"]
        assert path.read_text(encoding="utf-8") == "value = 1\n"
        gate.decide(approval_id, True)
        result = await operation
        return result

    result = asyncio.run(scenario())

    assert result.success is True
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    assert "@@" in result.output["diff"]
    assert result.metadata["change_id"]
    assert [event.type for event in events[:2]] == [
        AgentEventType.APPROVAL_REQUESTED,
        AgentEventType.APPROVAL_RESOLVED,
    ]

    change_id = result.metadata["change_id"]
    changes.revert(change_id)
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_apply_patch_dry_run_does_not_request_approval_or_create_undo(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    gate = ApprovalGate()
    changes = ChangeStore(Workspace(tmp_path))
    events = []
    executor = ToolExecutor(
        ToolRegistry(EDIT_TOOLS),
        event_handler=events.append,
        approval_gate=gate,
        change_store=changes,
    )

    result = asyncio.run(
        executor.execute(
            ToolCall(
                id="dry-run-patch",
                name="apply_patch",
                arguments={
                    "dry_run": True,
                    "patch": """--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-value = 1
+value = 2
""",
                },
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.output["dry_run"] is True
    assert path.read_text(encoding="utf-8") == "value = 1\n"
    assert not changes.records
    assert AgentEventType.APPROVAL_REQUESTED not in [event.type for event in events]


def test_rejected_edit_never_touches_the_workspace(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    gate = ApprovalGate()
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        approval_gate=gate,
        change_store=ChangeStore(Workspace(tmp_path)),
    )

    async def scenario():
        operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="edit-2",
                    name="write_file",
                    arguments={"path": "main.py", "content": "unsafe\n"},
                ),
                make_context(tmp_path),
            )
        )
        await asyncio.sleep(0)
        approval_id = next(iter(gate._pending))
        gate.decide(approval_id, False)
        return await operation

    result = asyncio.run(scenario())

    assert result.success is False
    assert result.error == "operation rejected by user"
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_multiple_edits_in_one_turn_share_one_undo_record(tmp_path: Path) -> None:
    changes = ChangeStore(Workspace(tmp_path))
    context = make_context(tmp_path)
    context.turn_id = "turn-1"
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        change_store=changes,
    )

    async def scenario():
        first = await executor.execute(
            ToolCall(
                id="write-first",
                name="write_file",
                arguments={"path": "add.c", "content": "int main(void) {\n}\n"},
            ),
            context,
        )
        second = await executor.execute(
            ToolCall(
                id="write-second",
                name="write_file",
                arguments={"path": "add.c", "content": "int main(void) { return 0; }\n"},
            ),
            context,
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert first.metadata["change_id"] == second.metadata["change_id"]
    assert "return 0" in second.output["diff"]
    changes.revert(second.metadata["change_id"])
    assert not (tmp_path / "add.c").exists()


def test_shell_command_waits_for_approval_before_execution(tmp_path: Path) -> None:
    gate = ApprovalGate()
    executor = ToolExecutor(ToolRegistry(COMMAND_TOOLS), approval_gate=gate)
    context = make_context(tmp_path)

    async def scenario():
        operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="command-1",
                    name="execute_command",
                    arguments={"command": [sys.executable, "-c", "print('approved')"]},
                ),
                context,
            )
        )
        await asyncio.sleep(0)
        approval_id = next(iter(gate._pending))
        gate.decide(approval_id, True)
        return await operation

    result = asyncio.run(scenario())

    assert result.success is True
    assert result.output["stdout"].strip() == "approved"


def test_auto_approval_is_scoped_to_the_current_turn(tmp_path: Path) -> None:
    gate = ApprovalGate()
    events = []
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        event_handler=events.append,
        approval_gate=gate,
    )
    context = make_context(tmp_path)
    context.turn_id = "turn-1"

    async def scenario():
        first_operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="write-first",
                    name="write_file",
                    arguments={"path": "one.txt", "content": "one\n"},
                ),
                context,
            )
        )
        await asyncio.sleep(0)
        first_approval = next(iter(gate._pending))
        gate.decide(first_approval, True, approve_current_turn=True)
        first = await first_operation
        second = await executor.execute(
            ToolCall(
                id="write-second",
                name="write_file",
                arguments={"path": "two.txt", "content": "two\n"},
            ),
            context,
        )
        gate.finish_turn(context.turn_id)
        context.turn_id = "turn-2"
        third_operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="write-third",
                    name="write_file",
                    arguments={"path": "three.txt", "content": "three\n"},
                ),
                context,
            )
        )
        await asyncio.sleep(0)
        third_approval = next(iter(gate._pending))
        gate.decide(third_approval, True)
        third = await third_operation
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first.success is True
    assert second.success is True
    assert third.success is True
    requested = [event for event in events if event.type is AgentEventType.APPROVAL_REQUESTED]
    resolved = [event for event in events if event.type is AgentEventType.APPROVAL_RESOLVED]
    assert [event.payload["automatic"] for event in requested] == [False, True, False]
    assert [event.payload["automatic"] for event in resolved] == [False, True, False]
    assert resolved[0].payload["approval_scope"] == "current_turn"
    assert resolved[1].payload["approval_scope"] == "current_turn"
    assert resolved[2].payload["approval_scope"] == "single_action"


def test_session_auto_approval_covers_multiple_turns_until_disabled(tmp_path: Path) -> None:
    gate = ApprovalGate()
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        approval_gate=gate,
    )
    context = make_context(tmp_path)
    gate.set_auto_approval(True)

    async def scenario():
        context.turn_id = "turn-1"
        first = await executor.execute(
            ToolCall(
                id="session-write-1",
                name="write_file",
                arguments={"path": "one.txt", "content": "one\n"},
            ),
            context,
        )
        context.turn_id = "turn-2"
        second = await executor.execute(
            ToolCall(
                id="session-write-2",
                name="write_file",
                arguments={"path": "two.txt", "content": "two\n"},
            ),
            context,
        )
        gate.set_auto_approval(False)
        third_operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="session-write-3",
                    name="write_file",
                    arguments={"path": "three.txt", "content": "three\n"},
                ),
                context,
            )
        )
        await asyncio.sleep(0)
        third_approval = next(iter(gate._pending))
        gate.decide(third_approval, True)
        third = await third_operation
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first.success is True
    assert second.success is True
    assert third.success is True
    assert not gate._pending


def test_cancelling_an_approval_wait_does_not_execute_the_tool(tmp_path: Path) -> None:
    gate = ApprovalGate()
    executor = ToolExecutor(ToolRegistry(EDIT_TOOLS), approval_gate=gate)
    context = make_context(tmp_path)
    cancellation = asyncio.Event()
    context.cancellation_token = cancellation

    async def scenario():
        operation = asyncio.create_task(
            executor.execute(
                ToolCall(
                    id="edit-cancelled",
                    name="write_file",
                    arguments={"path": "never-created.txt", "content": "nope"},
                ),
                context,
            )
        )
        await asyncio.sleep(0)
        cancellation.set()
        return await operation

    try:
        asyncio.run(scenario())
    except asyncio.CancelledError:
        pass
    assert not (tmp_path / "never-created.txt").exists()


def test_agent_final_answer_includes_edit_diff(tmp_path: Path) -> None:
    gate = ApprovalGate()
    events = []

    async def on_event(event):
        events.append(event)
        if event.type is AgentEventType.APPROVAL_REQUESTED:
            gate.decide(event.payload["approval_id"], True)

    session = Session(session_id="session-1", workspace_root=tmp_path)
    executor = ToolExecutor(
        ToolRegistry(FILESYSTEM_TOOLS),
        approval_gate=gate,
        change_store=ChangeStore(Workspace(tmp_path)),
        event_handler=on_event,
    )
    agent = Agent(
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
                ModelResponse(content="Created the file."),
            ]
        ),
        executor,
        ContextManager("You are a coding agent."),
        session,
        event_handler=on_event,
    )

    result = asyncio.run(agent.run_turn("Create hello.txt."))

    assert result.status is SessionStatus.COMPLETED
    assert result.final_answer is not None
    assert "## Changes" in result.final_answer
    assert "diff --git a/hello.txt b/hello.txt" in result.final_answer
    assert "+++ b/hello.txt" in result.final_answer


def test_rejected_side_effect_is_not_retried_indefinitely(tmp_path: Path) -> None:
    gate = ApprovalGate()
    events = []

    async def on_event(event):
        events.append(event)
        if event.type is AgentEventType.APPROVAL_REQUESTED:
            gate.decide(event.payload["approval_id"], False)

    session = Session(session_id="session-1", workspace_root=tmp_path)
    provider = MockModelProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "blocked.txt", "content": "nope"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write-2",
                        name="write_file",
                        arguments={"path": "blocked.txt", "content": "nope"},
                    )
                ]
            ),
        ]
    )
    agent = Agent(
        provider,
        ToolExecutor(
            ToolRegistry(FILESYSTEM_TOOLS),
            approval_gate=gate,
            change_store=ChangeStore(Workspace(tmp_path)),
            event_handler=on_event,
        ),
        ContextManager("You are a coding agent."),
        session,
        event_handler=on_event,
    )

    result = asyncio.run(agent.run_turn("Create blocked.txt."))

    assert result.status is SessionStatus.FAILED
    assert "without retrying" in result.error
    assert len(provider.requests) == 1
    assert not (tmp_path / "blocked.txt").exists()
