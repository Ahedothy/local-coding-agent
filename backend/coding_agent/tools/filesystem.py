"""Local filesystem tools constrained by the configured Workspace."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from .base import Tool, ToolContext, ToolResult


MAX_READ_BYTES = 1024 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_WRITE_CHARS = 1024 * 1024
DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_MATCHES = 50

IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


class ListFilesArguments(BaseModel):
    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=DEFAULT_MAX_ENTRIES, gt=0, le=1_000)


class ReadFileArguments(BaseModel):
    path: str
    start_line: int | None = Field(default=None, gt=0)
    end_line: int | None = Field(default=None, gt=0)
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, gt=0, le=100_000)

    @model_validator(mode="after")
    def validate_line_range(self) -> "ReadFileArguments":
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class WriteFileArguments(BaseModel):
    path: str
    content: str = Field(max_length=MAX_WRITE_CHARS)
    overwrite: bool = True
    create_parent_dirs: bool = False


class SearchFilesArguments(BaseModel):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str | None = None
    max_matches: int = Field(default=DEFAULT_MAX_MATCHES, gt=0, le=1_000)


def _relative_path(workspace_root: Path, path: Path) -> str:
    return path.relative_to(workspace_root).as_posix() or "."


def _is_binary(path: Path) -> bool:
    with path.open("rb") as file:
        sample = file.read(8_192)
    return b"\x00" in sample


def _read_text_limited(path: Path, max_bytes: int) -> tuple[str, bool]:
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8"), truncated


def _tool_success(tool: Tool, output: object) -> ToolResult:
    return ToolResult(
        tool_call_id="pending",
        tool_name=tool.name,
        success=True,
        output=output,
    )


class ListFilesTool(Tool):
    name: ClassVar[str] = "list_files"
    description: ClassVar[str] = "List text and source files inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = ListFilesArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ListFilesArguments,
    ) -> ToolResult:
        directory = context.workspace.resolve_existing_path(arguments.path)
        if not directory.is_dir():
            raise ValueError(f"path is not a directory: {arguments.path}")

        files: list[str] = []
        pending = [directory]
        while pending and len(files) < arguments.max_entries:
            current = pending.pop(0)
            for entry in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
                if entry.name.casefold() in IGNORED_DIRECTORY_NAMES:
                    continue
                if entry.is_symlink():
                    continue
                resolved = context.workspace.resolve_path(entry)
                if resolved.is_dir():
                    if arguments.recursive:
                        pending.append(resolved)
                elif resolved.is_file():
                    files.append(_relative_path(context.workspace.root, resolved))
                    if len(files) >= arguments.max_entries:
                        break

        return _tool_success(
            self,
            {
                "path": _relative_path(context.workspace.root, directory),
                "files": files,
                "truncated": bool(pending) or len(files) >= arguments.max_entries,
            },
        )


class ReadFileTool(Tool):
    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = "Read a UTF-8 text file inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = ReadFileArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ReadFileArguments,
    ) -> ToolResult:
        path = context.workspace.ensure_readable_file(arguments.path)
        if _is_binary(path):
            raise ValueError(f"binary file is not supported: {arguments.path}")

        text, byte_truncated = _read_text_limited(path, MAX_READ_BYTES)
        lines = text.splitlines(keepends=True)
        start = arguments.start_line or 1
        end = arguments.end_line or len(lines)
        selected = "".join(lines[start - 1 : end])
        char_truncated = len(selected) > arguments.max_chars
        if char_truncated:
            selected = selected[: arguments.max_chars]

        return _tool_success(
            self,
            {
                "path": _relative_path(context.workspace.root, path),
                "content": selected,
                "start_line": start,
                "end_line": min(end, len(lines)),
                "truncated": byte_truncated or char_truncated,
            },
        )


class WriteFileTool(Tool):
    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = "Write UTF-8 text to a file inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = WriteFileArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: WriteFileArguments,
    ) -> ToolResult:
        path = context.workspace.ensure_writable_file(arguments.path)
        if path.exists() and not arguments.overwrite:
            raise FileExistsError(f"file already exists: {arguments.path}")

        if path.parent.exists() and not path.parent.is_dir():
            raise ValueError(f"parent path is not a directory: {path.parent}")
        if not path.parent.exists():
            if not arguments.create_parent_dirs:
                raise FileNotFoundError(f"parent directory does not exist: {path.parent}")
            path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(arguments.content, encoding="utf-8")
        return _tool_success(
            self,
            {
                "path": _relative_path(context.workspace.root, path),
                "bytes_written": len(arguments.content.encode("utf-8")),
            },
        )


class SearchFilesTool(Tool):
    name: ClassVar[str] = "search_files"
    description: ClassVar[str] = "Search UTF-8 text files inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = SearchFilesArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: SearchFilesArguments,
    ) -> ToolResult:
        scope = context.workspace.resolve_existing_path(arguments.path)
        if not scope.is_dir():
            raise ValueError(f"search path is not a directory: {arguments.path}")

        matches: list[str] = []
        pending = [scope]
        while pending and len(matches) < arguments.max_matches:
            current = pending.pop(0)
            for entry in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
                if entry.name.casefold() in IGNORED_DIRECTORY_NAMES:
                    continue
                if entry.is_symlink():
                    continue
                resolved = context.workspace.resolve_path(entry)
                if resolved.is_dir():
                    pending.append(resolved)
                    continue
                if not resolved.is_file() or resolved.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                if arguments.glob is not None and not resolved.match(arguments.glob):
                    continue
                if _is_binary(resolved):
                    continue
                try:
                    text, _ = _read_text_limited(resolved, MAX_SEARCH_FILE_BYTES)
                except UnicodeDecodeError:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if arguments.query in line:
                        matches.append(
                            f"{_relative_path(context.workspace.root, resolved)}:{line_number}: {line}"
                        )
                        if len(matches) >= arguments.max_matches:
                            break
                if len(matches) >= arguments.max_matches:
                    break

        return _tool_success(
            self,
            {
                "query": arguments.query,
                "matches": matches,
                "truncated": bool(pending) or len(matches) >= arguments.max_matches,
            },
        )


FILESYSTEM_TOOLS: tuple[Tool, ...] = (
    ListFilesTool(),
    ReadFileTool(),
    WriteFileTool(),
    SearchFilesTool(),
)
