"""Local subprocess tool constrained by the configured Workspace."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, StrictStr

from .base import Tool, ToolContext, ToolResult


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
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
        started_at = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *arguments.command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                stdout_bytes, stderr_bytes = b"", b""
            stdout, stdout_truncated = _decode_output(stdout_bytes, arguments.max_output_chars)
            stderr, stderr_truncated = _decode_output(stderr_bytes, arguments.max_output_chars)
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=f"command timed out after {arguments.timeout_seconds} seconds",
                output={
                    "command": arguments.command,
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
            process.kill()
            await process.communicate()
            raise

        stdout, stdout_truncated = _decode_output(stdout_bytes, arguments.max_output_chars)
        stderr, stderr_truncated = _decode_output(stderr_bytes, arguments.max_output_chars)
        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "command": arguments.command,
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
