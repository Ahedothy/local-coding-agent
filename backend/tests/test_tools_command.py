from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def execute_command(tmp_path: Path, arguments: dict) -> object:
    executor = ToolExecutor(ToolRegistry(COMMAND_TOOLS))
    return asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="execute_command", arguments=arguments),
            make_context(tmp_path),
        )
    )


def python_command(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_execute_command_runs_locally_with_workspace_cwd(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": python_command("import os; print(os.getcwd())")},
    )

    assert result.success is True
    assert result.output["returncode"] == 0
    assert Path(result.output["stdout"].strip()).resolve() == tmp_path.resolve()
    assert result.output["cwd"] == "."
    assert result.output["timed_out"] is False


def test_execute_command_returns_nonzero_exit_and_output(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": python_command("import sys; print('bad stdout'); print('bad stderr', file=sys.stderr); sys.exit(3)")},
    )

    assert result.success is True
    assert result.output["returncode"] == 3
    assert "bad stdout" in result.output["stdout"]
    assert "bad stderr" in result.output["stderr"]


def test_execute_command_times_out(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": python_command("import time; time.sleep(2)"), "timeout_seconds": 0.1},
    )

    assert result.success is False
    assert result.output["timed_out"] is True
    assert "timed out" in result.error


def test_execute_command_truncates_stdout_and_stderr(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {
            "command": python_command("import sys; print('x' * 100); print('y' * 100, file=sys.stderr)"),
            "max_output_chars": 10,
        },
    )

    assert result.success is True
    assert len(result.output["stdout"]) == 10
    assert len(result.output["stderr"]) == 10
    assert result.output["stdout_truncated"] is True
    assert result.output["stderr_truncated"] is True


def test_execute_command_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": python_command("print('unsafe')"), "cwd": ".."},
    )

    assert result.success is False
    assert "outside the workspace" in result.error


def test_execute_command_blocks_obviously_dangerous_command(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": ["rm", "-rf", "."]},
    )

    assert result.success is False
    assert "blocked for safety" in result.error

