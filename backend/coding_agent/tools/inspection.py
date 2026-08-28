"""Read-only inspection tools for the configured local Workspace."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult
from .filesystem import IGNORED_DIRECTORY_NAMES, _is_binary, _relative_path


MAX_TREE_ENTRIES = 1_000
MAX_TREE_DEPTH = 12
MAX_INFO_SCAN_BYTES = 4 * 1024 * 1024
DEFAULT_TREE_ENTRIES = 200
DEFAULT_TREE_DEPTH = 4
DEFAULT_DIFF_CHARS = 100_000


class GetFileInfoArguments(BaseModel):
    path: str = Field(description="Existing file or directory path inside the workspace.")


class ListDirectoryTreeArguments(BaseModel):
    path: str = Field(
        default=".",
        description="Existing directory path inside the workspace.",
    )
    max_depth: int = Field(
        default=DEFAULT_TREE_DEPTH,
        ge=0,
        le=MAX_TREE_DEPTH,
        description="Maximum child depth to inspect.",
    )
    max_entries: int = Field(
        default=DEFAULT_TREE_ENTRIES,
        gt=0,
        le=MAX_TREE_ENTRIES,
        description="Maximum number of entries to return.",
    )


class GitDiffArguments(BaseModel):
    path: str = Field(
        default=".",
        description="Existing file or directory scope inside the workspace.",
    )
    max_chars: int = Field(
        default=DEFAULT_DIFF_CHARS,
        gt=0,
        le=MAX_INFO_SCAN_BYTES,
        description="Shared character budget for diff and status output.",
    )


def _success(tool: Tool, output: Any) -> ToolResult:
    return ToolResult(
        tool_call_id="pending",
        tool_name=tool.name,
        success=True,
        output=output,
    )


class GetFileInfoTool(Tool):
    name: ClassVar[str] = "get_file_info"
    description: ClassVar[str] = (
        "Return metadata for one existing local workspace file or directory."
    )
    parameters_model: ClassVar[type[BaseModel]] = GetFileInfoArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: GetFileInfoArguments,
    ) -> ToolResult:
        path = context.workspace.resolve_existing_path(arguments.path)
        stat = path.stat()
        is_file = path.is_file()
        is_binary = _is_binary(path) if is_file else False
        line_count: int | None = None
        line_count_truncated = False
        encoding: str | None = None

        if is_file and not is_binary:
            encoding = "utf-8"
            if stat.st_size <= MAX_INFO_SCAN_BYTES:
                try:
                    line_count = path.read_text(encoding="utf-8").count("\n")
                    if stat.st_size > 0 and not path.read_bytes().endswith((b"\n", b"\r")):
                        line_count += 1
                except UnicodeDecodeError:
                    is_binary = True
                    encoding = None
                    line_count = None
            else:
                line_count_truncated = True

        return _success(
            self,
            {
                "path": _relative_path(context.workspace.root, path),
                "kind": "file" if is_file else "directory",
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "is_binary": is_binary,
                "encoding": encoding,
                "line_count": line_count,
                "line_count_truncated": line_count_truncated,
            },
        )


class ListDirectoryTreeTool(Tool):
    name: ClassVar[str] = "list_directory_tree"
    description: ClassVar[str] = (
        "List a compact, deterministic directory tree inside the workspace."
    )
    parameters_model: ClassVar[type[BaseModel]] = ListDirectoryTreeArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ListDirectoryTreeArguments,
    ) -> ToolResult:
        root = context.workspace.resolve_existing_path(arguments.path)
        if not root.is_dir():
            raise ValueError(f"path is not a directory: {arguments.path}")

        entries: list[dict[str, Any]] = []
        tree_lines: list[str] = []
        truncated = False

        if arguments.max_depth == 0:
            try:
                truncated = any(
                    child.name.casefold() not in IGNORED_DIRECTORY_NAMES
                    and not child.is_symlink()
                    for child in root.iterdir()
                )
            except OSError:
                truncated = True
            return _success(
                self,
                {
                    "path": _relative_path(context.workspace.root, root),
                    "entries": entries,
                    "tree": "",
                    "max_depth": arguments.max_depth,
                    "truncated": truncated,
                },
            )

        def walk(directory: Path, depth: int) -> None:
            nonlocal truncated
            try:
                children = sorted(
                    (
                        entry
                        for entry in directory.iterdir()
                        if entry.name.casefold() not in IGNORED_DIRECTORY_NAMES
                        and not entry.is_symlink()
                    ),
                    key=lambda entry: (
                        not entry.is_dir(),
                        entry.name.casefold(),
                    ),
                )
            except OSError as exc:
                raise ValueError(f"could not inspect directory {directory}: {exc}") from exc

            for entry in children:
                if len(entries) >= arguments.max_entries:
                    truncated = True
                    return
                resolved = context.workspace.resolve_path(entry)
                is_directory = resolved.is_dir()
                relative = _relative_path(context.workspace.root, resolved)
                entries.append(
                    {
                        "path": relative,
                        "kind": "directory" if is_directory else "file",
                        "depth": depth + 1,
                    }
                )
                tree_lines.append(
                    f"{'  ' * depth}{Path(relative).name}"
                    + ("/" if is_directory else "")
                )
                if is_directory:
                    if depth + 1 < arguments.max_depth:
                        walk(resolved, depth + 1)
                    else:
                        try:
                            has_visible_child = any(
                                child.name.casefold() not in IGNORED_DIRECTORY_NAMES
                                and not child.is_symlink()
                                for child in resolved.iterdir()
                            )
                        except OSError:
                            has_visible_child = True
                        truncated = truncated or has_visible_child
                if truncated:
                    return

        walk(root, 0)
        return _success(
            self,
            {
                "path": _relative_path(context.workspace.root, root),
                "entries": entries,
                "tree": "\n".join(tree_lines),
                "max_depth": arguments.max_depth,
                "truncated": truncated,
            },
        )


def _run_git_command(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=timeout,
        check=False,
    )


class GitDiffTool(Tool):
    name: ClassVar[str] = "git_diff"
    description: ClassVar[str] = (
        "Read the Git diff and status for workspace changes without mutating Git."
    )
    parameters_model: ClassVar[type[BaseModel]] = GitDiffArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: GitDiffArguments,
    ) -> ToolResult:
        scope = context.workspace.resolve_existing_path(arguments.path)
        scope_path = _relative_path(context.workspace.root, scope)
        pathspec = "." if scope_path == "." else scope_path

        try:
            diff_result, status_result = await asyncio.gather(
                asyncio.to_thread(
                    _run_git_command,
                    ["git", "diff", "--no-ext-diff", "--unified=3", "HEAD", "--", pathspec],
                    context.workspace.root,
                    10.0,
                ),
                asyncio.to_thread(
                    _run_git_command,
                    ["git", "status", "--short", "--untracked-files=all", "--", pathspec],
                    context.workspace.root,
                    10.0,
                ),
            )
        except FileNotFoundError as exc:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error="git executable is not available",
                output={"path": scope_path, "repository": False, "diff": "", "status": []},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error="git diff inspection timed out",
                output={"path": scope_path, "repository": None, "diff": "", "status": []},
            )

        if diff_result.returncode != 0 or status_result.returncode != 0:
            stderr = (diff_result.stderr or status_result.stderr).decode("utf-8", errors="replace").strip()
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=stderr or "workspace is not a Git repository",
                output={
                    "path": scope_path,
                    "repository": False,
                    "diff": "",
                    "status": [],
                },
            )

        raw_diff_text = diff_result.stdout.decode("utf-8", errors="replace")
        raw_status_text = status_result.stdout.decode("utf-8", errors="replace")
        diff_truncated = len(raw_diff_text) > arguments.max_chars
        diff_text = raw_diff_text[: arguments.max_chars]
        remaining_chars = max(0, arguments.max_chars - len(diff_text))
        status_truncated = len(raw_status_text) > remaining_chars
        status_text = raw_status_text[:remaining_chars]
        return _success(
            self,
            {
                "path": scope_path,
                "repository": True,
                "diff": diff_text,
                "diff_truncated": diff_truncated,
                "status": status_text.splitlines(),
                "status_truncated": status_truncated,
                "has_changes": bool(raw_diff_text or raw_status_text),
            },
        )


INSPECTION_TOOLS: tuple[Tool, ...] = (
    GetFileInfoTool(),
    ListDirectoryTreeTool(),
    GitDiffTool(),
)
