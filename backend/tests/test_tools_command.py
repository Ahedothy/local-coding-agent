from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools import command as command_module
from coding_agent.tools.command import COMMAND_TOOLS, ExecuteCommandArguments
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

    assert result.success is False
    assert result.error == "command exited with return code 3"
    assert result.output["returncode"] == 3
    assert "bad stdout" in result.output["stdout"]
    assert "bad stderr" in result.output["stderr"]


def test_execute_command_reports_missing_executable_structurally(tmp_path: Path) -> None:
    result = execute_command(tmp_path, {"command": ["definitely-not-installed"]})

    assert result.success is False
    assert result.output["returncode"] is None
    assert result.output["executable_found"] is False
    assert "could not start command" in result.error


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


def test_execute_command_accepts_json_encoded_argument_array(tmp_path: Path) -> None:
    result = execute_command(
        tmp_path,
        {"command": json.dumps(python_command("print('ok')"))},
    )

    assert result.success is True
    assert result.output["stdout"].strip() == "ok"


def test_execute_command_still_rejects_plain_shell_string() -> None:
    with pytest.raises(ValidationError):
        ExecuteCommandArguments.model_validate({"command": "python -m pytest"})


def test_execute_command_cancellation_bounds_process_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.kill_called = False
            self.communicate_calls = 0

        def kill(self) -> None:
            self.kill_called = True

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise asyncio.CancelledError
            await asyncio.Event().wait()
            return b"", b""

    fake_process = FakeProcess()

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        return fake_process

    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(command_module, "PROCESS_CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def run() -> None:
        executor = ToolExecutor(ToolRegistry(COMMAND_TOOLS))
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(
                ToolCall(
                    id="call-1",
                    name="execute_command",
                    arguments={"command": ["python", "-c", "pass"]},
                ),
                make_context(tmp_path),
            )

    asyncio.run(run())
    assert fake_process.kill_called is True
    assert fake_process.communicate_calls == 2
