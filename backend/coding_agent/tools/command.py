"""Local subprocess tool constrained by the configured Workspace."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, StrictStr, field_validator

from .base import Tool, ToolContext, ToolResult


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
PROCESS_CLEANUP_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_CHARS = 100_000

BLOCKED_EXECUTABLES = frozenset(
    {
        "del",
        "diskpart",
        "erase",
        "format",
        "mkfs",
        "poweroff",
        "reboot",
        "rm",
        "rmdir",
        "shutdown",
    }
)


class ExecuteCommandArguments(BaseModel):
    command: list[StrictStr] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_TIMEOUT_SECONDS,
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        gt=0,
        le=MAX_OUTPUT_CHARS,
    )

    @field_validator("command", mode="before")
    @classmethod
    def decode_json_array(cls, value: object) -> object:
        """Accept model-produced JSON arrays without accepting shell strings."""
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("command must be a JSON array of argument strings") from exc
        if not isinstance(decoded, list):
            raise ValueError("command must be a JSON array of argument strings")
        return decoded


def _relative_path(workspace_root: Path, path: Path) -> str:
    return path.relative_to(workspace_root).as_posix() or "."


def _executable_name(command: str) -> str:
    return Path(command).name.casefold()


def _is_blocked(command: list[str]) -> bool:
    executable = _executable_name(command[0])
    return executable in BLOCKED_EXECUTABLES


def _decode_output(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    return text[:max_chars], truncated


def _normalize_local_windows_executable(command: list[str], cwd: Path) -> list[str]:
    """Resolve a workspace-local executable explicitly on Windows.

    ``CreateProcess`` is less forgiving than an interactive shell for local
    executables. If the model asks for ``hello.exe`` and that file exists in
    the validated cwd, use an explicit relative path. The original arguments
    are left untouched on other platforms or for PATH-resolved commands.
    """
    if os.name != "nt" or not command:
        return command
    executable = Path(command[0])
    if executable.is_absolute() or executable.parent != Path("."):
        return command
    candidates = [cwd / executable]
    if executable.suffix.casefold() != ".exe":
        candidates.append(cwd / f"{executable.name}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return [f".\\{candidate.name}", *command[1:]]
    return command


async def _cleanup_process(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """Kill a process and bound the wait for its pipes to close."""
    try:
        process.kill()
    except ProcessLookupError:
        pass

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
        return stdout_bytes, stderr_bytes
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Cleanup is best effort. The caller must retain the original outcome.
        return b"", b""


class ExecuteCommandTool(Tool):
    name: ClassVar[str] = "execute_command"
    description: ClassVar[str] = "Run a non-interactive command locally inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = ExecuteCommandArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ExecuteCommandArguments,
    ) -> ToolResult:
        if _is_blocked(arguments.command):
            raise ValueError(
                f"command is blocked for safety: {_executable_name(arguments.command[0])}"
            )

        cwd = context.workspace.resolve_cwd(arguments.cwd)
        command = _normalize_local_windows_executable(arguments.command, cwd)
        started_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=f"could not start command: {exc}",
                output={
                    "command": command,
                    "requested_command": arguments.command,
                    "cwd": _relative_path(context.workspace.root, cwd),
                    "returncode": None,
                    "stdout": "",
                    "stderr": str(exc),
                    "duration_seconds": time.monotonic() - started_at,
                    "timed_out": False,
                    "executable_found": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except asyncio.TimeoutError:
            stdout_bytes, stderr_bytes = await _cleanup_process(process)
            stdout, stdout_truncated = _decode_output(stdout_bytes, arguments.max_output_chars)
            stderr, stderr_truncated = _decode_output(stderr_bytes, arguments.max_output_chars)
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=f"command timed out after {arguments.timeout_seconds} seconds",
                output={
                    "command": command,
                    "cwd": _relative_path(context.workspace.root, cwd),
                    "returncode": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "duration_seconds": time.monotonic() - started_at,
                    "timed_out": True,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                },
            )
        except asyncio.CancelledError:
            await _cleanup_process(process)
            raise

        stdout, stdout_truncated = _decode_output(stdout_bytes, arguments.max_output_chars)
        stderr, stderr_truncated = _decode_output(stderr_bytes, arguments.max_output_chars)
        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=process.returncode == 0,
            error=(
                None
                if process.returncode == 0
                else f"command exited with return code {process.returncode}"
            ),
            output={
                "command": command,
                "cwd": _relative_path(context.workspace.root, cwd),
                "returncode": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started_at,
                "timed_out": False,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        )


COMMAND_TOOLS: tuple[Tool, ...] = (ExecuteCommandTool(),)
