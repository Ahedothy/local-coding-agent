"""Local filesystem tools constrained by the configured Workspace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from .base import Tool, ToolContext, ToolResult
from .file_version import sha256_bytes


MAX_READ_BYTES = 1024 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_WRITE_CHARS = 1024 * 1024
DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_MATCHES = 50
MAX_SEARCH_LINE_CHARS = 500

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
    expected_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 returned by a previous read of this file.",
    )


class SearchFilesArguments(BaseModel):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str | None = None
    max_matches: int = Field(default=DEFAULT_MAX_MATCHES, gt=0, le=1_000)
    use_regex: bool = Field(
        default=False,
        description="Treat query as a Python regular expression when true.",
    )
    case_sensitive: bool = Field(
        default=True,
        description="Use case-sensitive matching when true.",
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Number of lines before and after each match to include.",
    )


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


def _line_match(
    line: str,
    query: str,
    *,
    use_regex: bool,
    case_sensitive: bool,
    pattern: re.Pattern[str] | None,
) -> tuple[int, int] | None:
    if use_regex:
        if pattern is None:
            return None
        match = pattern.search(line)
        if match is None:
            return None
        return match.start(), match.end()

    haystack = line if case_sensitive else line.casefold()
    needle = query if case_sensitive else query.casefold()
    start = haystack.find(needle)
    if start < 0:
        return None
    return start, start + len(query)


def _context_window(
    lines: list[str],
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for index in range(max(0, start), min(len(lines), end)):
        excerpt, _, _, truncated = _line_excerpt(lines[index], None)
        context.append(
            {
                "line_number": index + 1,
                "line": excerpt,
                "line_truncated": truncated,
            }
        )
    return context


def _line_excerpt(
    line: str,
    span: tuple[int, int] | None,
) -> tuple[str, int | None, int | None, bool]:
    if len(line) <= MAX_SEARCH_LINE_CHARS:
        if span is None:
            return line, None, None, False
        return line, span[0], span[1], False

    if span is None:
        return f"{line[:MAX_SEARCH_LINE_CHARS]}...", None, None, True

    match_start, match_end = span
    half_window = max(1, (MAX_SEARCH_LINE_CHARS - 6) // 2)
    start = max(0, match_start - half_window)
    end = min(len(line), match_end + half_window)
    if end - start > MAX_SEARCH_LINE_CHARS:
        end = start + MAX_SEARCH_LINE_CHARS
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(line) else ""
    excerpt = f"{prefix}{line[start:end]}{suffix}"
    adjusted_start = match_start - start + len(prefix)
    adjusted_end = match_end - start + len(prefix)
    return excerpt, adjusted_start, adjusted_end, True


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
        original = path.read_bytes()
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
                "sha256": sha256_bytes(original),
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
        from .file_version import ensure_expected_sha256

        ensure_expected_sha256(path, arguments.expected_sha256, arguments.path)
        if path.exists() and not arguments.overwrite:
            raise FileExistsError(f"file already exists: {arguments.path}")

        if path.parent.exists() and not path.parent.is_dir():
            raise ValueError(f"parent path is not a directory: {path.parent}")
        if not path.parent.exists():
            if not arguments.create_parent_dirs:
                raise FileNotFoundError(f"parent directory does not exist: {path.parent}")
            path.parent.mkdir(parents=True, exist_ok=True)

        # Recheck immediately before the side effect to narrow the race window.
        ensure_expected_sha256(path, arguments.expected_sha256, arguments.path)
        path.write_text(arguments.content, encoding="utf-8")
        return _tool_success(
            self,
            {
                "path": _relative_path(context.workspace.root, path),
                "bytes_written": len(arguments.content.encode("utf-8")),
                "sha256": sha256_bytes(arguments.content.encode("utf-8")),
            },
        )


class SearchFilesTool(Tool):
    name: ClassVar[str] = "search_files"
    description: ClassVar[str] = (
        "Search UTF-8 text files inside the workspace. Supports literal or "
        "regular-expression matching, optional case-insensitive search, glob "
        "scoping, and small before/after context windows."
    )
    parameters_model: ClassVar[type[BaseModel]] = SearchFilesArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: SearchFilesArguments,
    ) -> ToolResult:
        scope = context.workspace.resolve_existing_path(arguments.path)
        if not scope.is_dir():
            raise ValueError(f"search path is not a directory: {arguments.path}")

        pattern: re.Pattern[str] | None = None
        if arguments.use_regex:
            flags = 0 if arguments.case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(arguments.query, flags)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc

        matches: list[str] = []
        structured_matches: list[dict[str, Any]] = []
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
                lines = text.splitlines()
                relative = _relative_path(context.workspace.root, resolved)
                for index, line in enumerate(lines):
                    span = _line_match(
                        line,
                        arguments.query,
                        use_regex=arguments.use_regex,
                        case_sensitive=arguments.case_sensitive,
                        pattern=pattern,
                    )
                    if span is None:
                        continue
                    line_number = index + 1
                    excerpt, match_start, match_end, line_truncated = _line_excerpt(
                        line,
                        span,
                    )
                    matches.append(f"{relative}:{line_number}: {excerpt}")
                    structured_matches.append(
                        {
                            "path": relative,
                            "line_number": line_number,
                            "line": excerpt,
                            "match_start": match_start,
                            "match_end": match_end,
                            "line_truncated": line_truncated,
                            "before_context": _context_window(
                                lines,
                                index - arguments.context_lines,
                                index,
                            ),
                            "after_context": _context_window(
                                lines,
                                index + 1,
                                index + 1 + arguments.context_lines,
                            ),
                        }
                    )
                    if len(matches) >= arguments.max_matches:
                        break
                if len(matches) >= arguments.max_matches:
                    break

        return _tool_success(
            self,
            {
                "query": arguments.query,
                "path": _relative_path(context.workspace.root, scope),
                "glob": arguments.glob,
                "use_regex": arguments.use_regex,
                "case_sensitive": arguments.case_sensitive,
                "context_lines": arguments.context_lines,
                "matches": matches,
                "structured_matches": structured_matches,
                "match_count": len(matches),
                "truncated": bool(pending) or len(matches) >= arguments.max_matches,
            },
        )


FILESYSTEM_TOOLS: tuple[Tool, ...] = (
    ListFilesTool(),
    ReadFileTool(),
    WriteFileTool(),
    SearchFilesTool(),
)
