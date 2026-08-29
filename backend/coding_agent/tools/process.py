"""Bounded local process lifecycle management for long-running tasks."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, StrictStr

from .base import Tool, ToolContext, ToolResult
from .command import (
    MAX_OUTPUT_CHARS,
    _executable_name,
    _is_blocked,
    _normalize_local_windows_executable,
)


MAX_ACTIVE_PROCESSES = 32
MAX_RETAINED_PROCESSES = 64
MAX_PROCESS_OUTPUT_CHARS = 100_000
DEFAULT_STARTUP_WAIT_SECONDS = 0.2
MAX_STARTUP_WAIT_SECONDS = 5.0
DEFAULT_STOP_GRACE_SECONDS = 2.0
MAX_STOP_GRACE_SECONDS = 10.0
MAX_INPUT_CHARS = 20_000


class ManageProcessArguments(BaseModel):
    """Arguments for one safe operation on a managed local process."""

    operation: Literal["start", "list", "status", "read", "write", "stop"]
    process_id: str | None = Field(
        default=None,
        description="Process handle returned by a previous start operation.",
    )
    command: list[StrictStr] | None = Field(
        default=None,
        min_length=1,
        description="Executable and arguments for start; shell strings are not accepted.",
    )
    cwd: str = Field(
        default=".",
        description="Existing working directory inside the workspace for start.",
    )
    input: str | None = Field(
        default=None,
        max_length=MAX_INPUT_CHARS,
        description="Exact text to send to stdin for write, without implicit shell parsing.",
    )
    startup_wait_seconds: float = Field(
        default=DEFAULT_STARTUP_WAIT_SECONDS,
        ge=0,
        le=MAX_STARTUP_WAIT_SECONDS,
        description="How long start waits before returning the initial status.",
    )
    grace_seconds: float = Field(
        default=DEFAULT_STOP_GRACE_SECONDS,
        ge=0,
        le=MAX_STOP_GRACE_SECONDS,
        description="How long stop waits after a graceful terminate request.",
    )
    force: bool = Field(
        default=False,
        description="Kill immediately for stop instead of requesting graceful termination.",
    )
    max_output_chars: int = Field(
        default=MAX_OUTPUT_CHARS,
        gt=0,
        le=MAX_OUTPUT_CHARS,
        description="Maximum stdout and stderr returned by read or status.",
    )
    clear_output: bool = Field(
        default=False,
        description="Clear buffered output after read or status returns it.",
    )


@dataclass(slots=True)
class _ManagedProcess:
    process_id: str
    command: list[str]
    cwd: str
    process: asyncio.subprocess.Process
    started_at: float
    stdout_chunks: deque[str] = field(default_factory=deque)
    stderr_chunks: deque[str] = field(default_factory=deque)
    stdout_chars: int = 0
    stderr_chars: int = 0
    reader_tasks: tuple[asyncio.Task[None], asyncio.Task[None]] | None = None


def _success(tool: Tool, output: Any) -> ToolResult:
    return ToolResult(
        tool_call_id="pending",
        tool_name=tool.name,
        success=True,
        output=output,
    )


def _status(process: _ManagedProcess) -> str:
    return "running" if process.process.returncode is None else "exited"


def _append_output(process: _ManagedProcess, stream: Literal["stdout", "stderr"], data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    if not text:
        return
    chunks = process.stdout_chunks if stream == "stdout" else process.stderr_chunks
    current_chars = process.stdout_chars if stream == "stdout" else process.stderr_chars
    chunks.append(text)
    current_chars += len(text)
    if current_chars > MAX_PROCESS_OUTPUT_CHARS:
        retained = "".join(chunks)[-MAX_PROCESS_OUTPUT_CHARS:]
        chunks.clear()
        chunks.append(retained)
        current_chars = len(retained)
    if stream == "stdout":
        process.stdout_chars = current_chars
    else:
        process.stderr_chars = current_chars


async def _read_stream(
    process: _ManagedProcess,
    stream: asyncio.StreamReader | None,
    name: Literal["stdout", "stderr"],
) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            _append_output(process, name, chunk)
    except (asyncio.CancelledError, ConnectionError):
        return


def _tail(chunks: deque[str], max_chars: int) -> tuple[str, bool]:
    value = "".join(chunks)
    truncated = len(value) > max_chars
    return value[-max_chars:], truncated


def _clear_output(process: _ManagedProcess) -> None:
    process.stdout_chunks.clear()
    process.stderr_chunks.clear()
    process.stdout_chars = 0
    process.stderr_chars = 0


def _process_payload(
    managed: _ManagedProcess,
    *,
    max_output_chars: int = MAX_OUTPUT_CHARS,
    include_output: bool = True,
    clear_output: bool = False,
) -> dict[str, Any]:
    stdout, stdout_truncated = _tail(managed.stdout_chunks, max_output_chars)
    stderr, stderr_truncated = _tail(managed.stderr_chunks, max_output_chars)
    payload = {
        "process_id": managed.process_id,
        "pid": managed.process.pid,
        "command": managed.command,
        "cwd": managed.cwd,
        "status": _status(managed),
        "returncode": managed.process.returncode,
        "uptime_seconds": max(0.0, time.monotonic() - managed.started_at),
        "output_buffered": bool(stdout or stderr),
    }
    if include_output:
        payload.update(
            {
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            }
        )
    if clear_output:
        _clear_output(managed)
    return payload


async def _finish_readers(managed: _ManagedProcess) -> None:
    if managed.reader_tasks is None:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*managed.reader_tasks, return_exceptions=True),
            timeout=0.5,
        )
    except asyncio.TimeoutError:
        for task in managed.reader_tasks:
            task.cancel()
        await asyncio.gather(*managed.reader_tasks, return_exceptions=True)


class ManageProcessTool(Tool):
    name: ClassVar[str] = "manage_process"
    description: ClassVar[str] = (
        "Start, inspect, read, write stdin to, or stop a bounded local process "
        "inside the workspace. Uses argument arrays, shell=False, bounded output, "
        "and explicit process handles; it never runs shell strings."
    )
    parameters_model: ClassVar[type[BaseModel]] = ManageProcessArguments

    def __init__(self) -> None:
        self._processes: dict[str, _ManagedProcess] = {}

    def _get_process(self, process_id: str | None) -> _ManagedProcess:
        if not process_id:
            raise ValueError("process_id is required for this operation")
        process = self._processes.get(process_id)
        if process is None:
            raise ValueError(f"unknown process_id: {process_id}")
        return process

    def _prune(self) -> None:
        if len(self._processes) <= MAX_RETAINED_PROCESSES:
            return
        finished = sorted(
            (
                process
                for process in self._processes.values()
                if process.process.returncode is not None
            ),
            key=lambda process: process.started_at,
        )
        for process in finished[: max(0, len(self._processes) - MAX_RETAINED_PROCESSES)]:
            self._processes.pop(process.process_id, None)

    async def _start(
        self,
        context: ToolContext,
        arguments: ManageProcessArguments,
    ) -> ToolResult:
        if arguments.command is None:
            raise ValueError("command is required for start")
        if _is_blocked(arguments.command):
            raise ValueError(
                f"command is blocked for safety: {_executable_name(arguments.command[0])}"
            )
        running_count = sum(
            process.process.returncode is None for process in self._processes.values()
        )
        if running_count >= MAX_ACTIVE_PROCESSES:
            raise ValueError(f"maximum active process limit reached: {MAX_ACTIVE_PROCESSES}")

        cwd_path = context.workspace.resolve_cwd(arguments.cwd)
        command = _normalize_local_windows_executable(arguments.command, cwd_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=f"could not start process: {exc}",
                output={
                    "command": command,
                    "cwd": str(arguments.cwd),
                    "status": "not_started",
                    "returncode": None,
                },
            )

        process_id = f"proc-{uuid4().hex[:12]}"
        managed = _ManagedProcess(
            process_id=process_id,
            command=command,
            cwd=arguments.cwd,
            process=process,
            started_at=time.monotonic(),
        )
        managed.reader_tasks = (
            asyncio.create_task(_read_stream(managed, process.stdout, "stdout")),
            asyncio.create_task(_read_stream(managed, process.stderr, "stderr")),
        )
        self._processes[process_id] = managed
        self._prune()
        if arguments.startup_wait_seconds:
            await asyncio.sleep(arguments.startup_wait_seconds)
        await asyncio.sleep(0)
        payload = _process_payload(managed, max_output_chars=arguments.max_output_chars)
        payload.update(
            {
                "operation": "start",
                "started": True,
                "startup_wait_seconds": arguments.startup_wait_seconds,
            }
        )
        return _success(self, payload)

    async def _list(self, arguments: ManageProcessArguments) -> ToolResult:
        processes = [
            _process_payload(
                process,
                max_output_chars=0,
                include_output=False,
            )
            for process in sorted(self._processes.values(), key=lambda item: item.started_at)
        ]
        return _success(self, {"operation": "list", "processes": processes, "count": len(processes)})

    async def _status_or_read(
        self,
        arguments: ManageProcessArguments,
        *,
        clear_output: bool,
    ) -> ToolResult:
        process = self._get_process(arguments.process_id)
        await asyncio.sleep(0)
        payload = _process_payload(
            process,
            max_output_chars=arguments.max_output_chars,
            clear_output=clear_output,
        )
        payload["operation"] = "read" if clear_output else "status"
        return _success(self, payload)

    async def _write(self, arguments: ManageProcessArguments) -> ToolResult:
        process = self._get_process(arguments.process_id)
        if process.process.returncode is not None:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error="cannot write to an exited process",
                output=_process_payload(process, include_output=False),
            )
        if process.process.stdin is None:
            raise ValueError("process stdin is unavailable")
        if arguments.input is None:
            raise ValueError("input is required for write")
        try:
            process.process.stdin.write(arguments.input.encode("utf-8"))
            await process.process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=f"could not write to process stdin: {exc}",
                output=_process_payload(process, include_output=False),
            )
        return _success(
            self,
            {
                "operation": "write",
                "process_id": process.process_id,
                "bytes_written": len(arguments.input.encode("utf-8")),
                "status": _status(process),
            },
        )

    async def _stop(self, arguments: ManageProcessArguments) -> ToolResult:
        process = self._get_process(arguments.process_id)
        if process.process.returncode is None:
            if arguments.force:
                process.process.kill()
            else:
                process.process.terminate()
            try:
                await asyncio.wait_for(process.process.wait(), timeout=arguments.grace_seconds)
            except asyncio.TimeoutError:
                process.process.kill()
                await process.process.wait()
        await _finish_readers(process)
        payload = _process_payload(
            process,
            max_output_chars=arguments.max_output_chars,
            clear_output=arguments.clear_output,
        )
        payload.update(
            {
                "operation": "stop",
                "stopped": True,
                "forced": arguments.force or process.process.returncode == -9,
            }
        )
        return _success(self, payload)

    async def execute(
        self,
        context: ToolContext,
        arguments: ManageProcessArguments,
    ) -> ToolResult:
        if arguments.operation == "start":
            return await self._start(context, arguments)
        if arguments.operation == "list":
            return await self._list(arguments)
        if arguments.operation == "status":
            return await self._status_or_read(arguments, clear_output=arguments.clear_output)
        if arguments.operation == "read":
            return await self._status_or_read(arguments, clear_output=True)
        if arguments.operation == "write":
            return await self._write(arguments)
        return await self._stop(arguments)


PROCESS_TOOLS: tuple[Tool, ...] = (ManageProcessTool(),)
