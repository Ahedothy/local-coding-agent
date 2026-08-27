from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from coding_agent.agent import Agent, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.workspace import Workspace


DEMO_SOURCE = Path(__file__).resolve().parents[2] / "demo" / "buggy_calculator"


def test_demo_agent_reads_tests_edits_code_and_retests(tmp_path: Path) -> None:
    workspace_root = tmp_path / "buggy_calculator"
    shutil.copytree(
        DEMO_SOURCE,
        workspace_root,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    python_executable = sys.executable
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(id="read-source", name="read_file", arguments={"path": "calculator.py"})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="read-tests", name="read_file", arguments={"path": "test_calculator.py"})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="run-before",
                    name="execute_command",
                    arguments={"command": [python_executable, "-m", "pytest", "-q"]},
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="fix-divide",
                    name="replace_in_file",
                    arguments={
                        "path": "calculator.py",
                        "old_text": "    if b == 0:\n        return 0",
                        "new_text": '    if b == 0:\n        raise ValueError("cannot divide by zero")',
                    },
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="fix-average",
                    name="replace_in_file",
                    arguments={
                        "path": "calculator.py",
                        "old_text": "def average(numbers: list[int | float]) -> float:\n    return sum(numbers) / len(numbers)",
                        "new_text": 'def average(numbers: list[int | float]) -> float:\n    if not numbers:\n        raise ValueError("cannot average an empty sequence")\n    return sum(numbers) / len(numbers)',
                    },
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="run-after",
                    name="execute_command",
                    arguments={"command": [python_executable, "-m", "pytest", "-q"]},
                )
            ]
        ),
        ModelResponse(content="Fixed both calculator edge cases; all tests pass."),
    ]
    provider = MockModelProvider(responses)
    events: list = []
    registry = ToolRegistry((*FILESYSTEM_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS))
    executor = ToolExecutor(registry, event_handler=events.append)
    session = Session(workspace_root=workspace_root)
    agent = Agent(
        provider,
        executor,
        ContextManager("You are a local coding agent."),
        session,
        tool_context=ToolContext(
            session_id=session.session_id,
            workspace=Workspace(workspace_root),
        ),
        event_handler=events.append,
    )

    result = asyncio.run(
        agent.run(
            "Fix the failing tests: read the code, run the tests, make the minimal "
            "change, and run the tests again."
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert result.final_answer == "Fixed both calculator edge cases; all tests pass."
    assert result.tool_calls == 6
    assert "raise ValueError(\"cannot divide by zero\")" in (
        workspace_root / "calculator.py"
    ).read_text(encoding="utf-8")
    assert "cannot average an empty sequence" in (
        workspace_root / "calculator.py"
    ).read_text(encoding="utf-8")
    assert [
        event.payload.get("tool_name")
        for event in events
        if event.type.value == "tool_finished"
    ] == [
        "read_file",
        "read_file",
        "execute_command",
        "replace_in_file",
        "replace_in_file",
        "execute_command",
    ]
