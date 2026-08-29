from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.cli import _build_agent
from coding_agent.models import MockModelProvider, ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.approval import requires_approval_for
from coding_agent.tools.process import ManageProcessTool, PROCESS_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def run_tool(
    tmp_path: Path,
    tool: ManageProcessTool,
    arguments: dict,
) -> object:
    return asyncio.run(
        ToolExecutor(ToolRegistry((tool,))).execute(
            ToolCall(id="call-1", name="manage_process", arguments=arguments),
            make_context(tmp_path),
        )
    )


def python_command(source: str) -> list[str]:
    return [sys.executable, "-u", "-c", source]


def test_process_tool_is_registered() -> None:
    assert [tool.name for tool in PROCESS_TOOLS] == ["manage_process"]


def test_each_agent_gets_an_isolated_process_manager(tmp_path: Path) -> None:
    first = _build_agent(Workspace(tmp_path), MockModelProvider([]), lambda event: None)
    second = _build_agent(Workspace(tmp_path), MockModelProvider([]), lambda event: None)

    assert first.tool_executor.registry.get("manage_process") is not None
    assert first.tool_executor.registry.get("manage_process") is not second.tool_executor.registry.get(
        "manage_process"
    )


def test_process_approval_is_required_only_for_side_effecting_operations() -> None:
    assert requires_approval_for("manage_process", {"operation": "start"}) is True
    assert requires_approval_for("manage_process", {"operation": "write"}) is True
    assert requires_approval_for("manage_process", {"operation": "stop"}) is True
    assert requires_approval_for("manage_process", {"operation": "list"}) is False
    assert requires_approval_for("manage_process", {"operation": "status"}) is False
    assert requires_approval_for("manage_process", {"operation": "read"}) is False


def test_process_lifecycle_supports_stdin_status_read_and_stop(tmp_path: Path) -> None:
    tool = ManageProcessTool()
    command = python_command(
        "import sys, time; print('ready', flush=True); "
        "line = sys.stdin.readline(); print('received=' + line.strip(), flush=True); "
        "time.sleep(30)"
    )

    async def scenario():
        executor = ToolExecutor(ToolRegistry((tool,)))
        context = make_context(tmp_path)

        async def call(arguments: dict) -> object:
            return await executor.execute(
                ToolCall(id="call-1", name="manage_process", arguments=arguments),
                context,
            )

        started = await call(
            {"operation": "start", "command": command, "startup_wait_seconds": 0.15}
        )
        assert started.success is True
        process_id = started.output["process_id"]
        try:
            assert started.output["status"] == "running"
            assert started.output["stdout"].replace("\r\n", "\n") == "ready\n"

            status = await call({"operation": "status", "process_id": process_id})
            assert status.success is True
            assert status.output["status"] == "running"
            assert status.output["pid"] is not None

            written = await call(
                {"operation": "write", "process_id": process_id, "input": "hello\n"}
            )
            assert written.success is True
            assert written.output["bytes_written"] == len("hello\n".encode("utf-8"))

            await asyncio.sleep(0.15)
            output = await call(
                {"operation": "read", "process_id": process_id, "max_output_chars": 1_000}
            )
            assert output.success is True
            assert "received=hello" in output.output["stdout"]
        finally:
            await call({"operation": "stop", "process_id": process_id, "force": True})

        return await call({"operation": "status", "process_id": process_id})

    stopped = asyncio.run(scenario())
    assert stopped.success is True
    assert stopped.output["status"] == "exited"


def test_process_list_is_bounded_and_does_not_include_output(tmp_path: Path) -> None:
    tool = ManageProcessTool()
    async def scenario():
        executor = ToolExecutor(ToolRegistry((tool,)))
        context = make_context(tmp_path)
        started = await executor.execute(
            ToolCall(
                id="start",
                name="manage_process",
                arguments={
                    "operation": "start",
                    "command": python_command(
                        "import time; print('running', flush=True); time.sleep(30)"
                    ),
                    "startup_wait_seconds": 0.05,
                },
            ),
            context,
        )
        process_id = started.output["process_id"]
        try:
            listed = await executor.execute(
                ToolCall(id="list", name="manage_process", arguments={"operation": "list"}),
                context,
            )
            assert listed.success is True
            assert listed.output["count"] == 1
            assert listed.output["processes"][0]["process_id"] == process_id
            assert "stdout" not in listed.output["processes"][0]
        finally:
            await executor.execute(
                ToolCall(
                    id="stop",
                    name="manage_process",
                    arguments={"operation": "stop", "process_id": process_id, "force": True},
                ),
                context,
            )

    asyncio.run(scenario())


def test_process_output_is_drained_and_bounded(tmp_path: Path) -> None:
    tool = ManageProcessTool()
    async def scenario():
        executor = ToolExecutor(ToolRegistry((tool,)))
        context = make_context(tmp_path)
        started = await executor.execute(
            ToolCall(
                id="start",
                name="manage_process",
                arguments={
                    "operation": "start",
                    "command": python_command(
                        "import time; print('x' * 150000, flush=True); time.sleep(30)"
                    ),
                    "startup_wait_seconds": 0.2,
                },
            ),
            context,
        )
        process_id = started.output["process_id"]
        try:
            output = await executor.execute(
                ToolCall(
                    id="read",
                    name="manage_process",
                    arguments={
                        "operation": "read",
                        "process_id": process_id,
                        "max_output_chars": 100,
                    },
                ),
                context,
            )
            assert output.success is True
            assert len(output.output["stdout"]) == 100
            assert output.output["stdout_truncated"] is True
        finally:
            await executor.execute(
                ToolCall(
                    id="stop",
                    name="manage_process",
                    arguments={"operation": "stop", "process_id": process_id, "force": True},
                ),
                context,
            )

    asyncio.run(scenario())


def test_process_management_rejects_unsafe_commands_and_paths(tmp_path: Path) -> None:
    tool = ManageProcessTool()
    blocked = run_tool(tmp_path, tool, {"operation": "start", "command": ["rm", "-rf", "."]})
    escaped = run_tool(
        tmp_path,
        tool,
        {"operation": "start", "command": python_command("pass"), "cwd": ".."},
    )
    invalid_command = run_tool(tmp_path, tool, {"operation": "start", "command": "python -V"})

    assert blocked.success is False
    assert "blocked for safety" in blocked.error
    assert escaped.success is False
    assert "outside the workspace" in escaped.error
    assert invalid_command.success is False
    assert "invalid arguments" in invalid_command.error


def test_process_management_reports_exited_process(tmp_path: Path) -> None:
    tool = ManageProcessTool()
    async def scenario():
        executor = ToolExecutor(ToolRegistry((tool,)))
        context = make_context(tmp_path)
        started = await executor.execute(
            ToolCall(
                id="start",
                name="manage_process",
                arguments={
                    "operation": "start",
                    "command": python_command("print('done', flush=True)"),
                    "startup_wait_seconds": 0.15,
                },
            ),
            context,
        )
        process_id = started.output["process_id"]
        assert started.output["status"] == "exited"
        assert started.output["returncode"] == 0
        status = await executor.execute(
            ToolCall(
                id="status",
                name="manage_process",
                arguments={"operation": "status", "process_id": process_id},
            ),
            context,
        )
        assert status.output["status"] == "exited"
        assert status.output["returncode"] == 0

    asyncio.run(scenario())
