"""Read-only inspection tools for the configured local Workspace."""

from __future__ import annotations

import asyncio
import mimetypes
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult
from .filesystem import IGNORED_DIRECTORY_NAMES, _relative_path


MAX_TREE_ENTRIES = 1_000
MAX_TREE_DEPTH = 12
MAX_INFO_SCAN_BYTES = 4 * 1024 * 1024
DEFAULT_TREE_ENTRIES = 200
DEFAULT_TREE_DEPTH = 4
DEFAULT_DIFF_CHARS = 100_000
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

_TEXT_ENCODING_CANDIDATES = (
    "utf-8",
    "gb18030",
    "big5",
    "shift_jis",
    "cp1252",
    "latin-1",
)

_EXTENSION_TYPES: dict[str, tuple[str, str, str]] = {
    ".py": ("source code", "Python source", "text/x-python"),
    ".js": ("source code", "JavaScript source", "text/javascript"),
    ".jsx": ("source code", "JavaScript JSX source", "text/jsx"),
    ".ts": ("source code", "TypeScript source", "text/typescript"),
    ".tsx": ("source code", "TypeScript TSX source", "text/tsx"),
    ".c": ("source code", "C source", "text/x-c"),
    ".h": ("source code", "C/C++ header", "text/x-c"),
    ".cc": ("source code", "C++ source", "text/x-c++src"),
    ".cpp": ("source code", "C++ source", "text/x-c++src"),
    ".java": ("source code", "Java source", "text/x-java-source"),
    ".go": ("source code", "Go source", "text/x-go"),
    ".rs": ("source code", "Rust source", "text/rust"),
    ".css": ("stylesheet", "CSS stylesheet", "text/css"),
    ".html": ("document", "HTML document", "text/html"),
    ".htm": ("document", "HTML document", "text/html"),
    ".json": ("data", "JSON data", "application/json"),
    ".yaml": ("data", "YAML data", "application/yaml"),
    ".yml": ("data", "YAML data", "application/yaml"),
    ".toml": ("configuration", "TOML configuration", "application/toml"),
    ".md": ("document", "Markdown document", "text/markdown"),
    ".txt": ("text", "Plain text", "text/plain"),
    ".sql": ("source code", "SQL source", "application/sql"),
    ".sh": ("source code", "Shell script", "application/x-sh"),
    ".ps1": ("source code", "PowerShell script", "text/x-powershell"),
}

_MAGIC_TYPES: tuple[tuple[bytes, str, str, str], ...] = (
    (b"%PDF-", "document", "PDF document", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image", "PNG image", "image/png"),
    (b"\xff\xd8\xff", "image", "JPEG image", "image/jpeg"),
    (b"GIF87a", "image", "GIF image", "image/gif"),
    (b"GIF89a", "image", "GIF image", "image/gif"),
    (b"RIFF", "image", "RIFF container", "image/*"),
    (b"PK\x03\x04", "archive", "ZIP archive", "application/zip"),
    (b"\x1f\x8b", "archive", "Gzip archive", "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", "archive", "7-Zip archive", "application/x-7z-compressed"),
    (b"\x7fELF", "executable", "ELF executable", "application/x-executable"),
    (b"MZ", "executable", "Windows executable", "application/vnd.microsoft.portable-executable"),
    (b"SQLite format 3\x00", "database", "SQLite database", "application/vnd.sqlite3"),
    (b"\x00asm", "executable", "WebAssembly module", "application/wasm"),
)

_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".bin",
        ".class",
        ".dll",
        ".exe",
        ".gif",
        ".gz",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".png",
        ".so",
        ".wasm",
        ".webp",
        ".zip",
    }
)


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


def _has_binary_controls(data: bytes) -> bool:
    """Detect binary-like control bytes without misclassifying UTF-16 BOM text."""
    if not data:
        return False
    if b"\x00" in data:
        return True
    controls = sum(
        byte < 32 and byte not in {9, 10, 12, 13}
        for byte in data
    )
    return controls / len(data) > 0.02


def _decode_strict(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def _detect_encoding(data: bytes) -> tuple[bool, str | None, str, str]:
    """Return text/binary status, encoding, confidence, and detection method."""
    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            decoded = _decode_strict(data, encoding)
            if decoded is not None:
                return True, encoding, "high", "bom"

    if _has_binary_controls(data):
        return False, None, "high", "binary_control_bytes"

    if _decode_strict(data, "utf-8") is not None:
        return True, "utf-8", "high", "utf8"

    # Python's standard library has no general-purpose encoding detector. Try
    # a small, explicit set and report the heuristic so callers can decide
    # whether a write should preserve the original encoding.
    for encoding in _TEXT_ENCODING_CANDIDATES[1:]:
        decoded = _decode_strict(data, encoding)
        if decoded is None:
            continue
        if encoding in {"latin-1", "cp1252"}:
            return True, encoding, "low", "decode_heuristic"
        return True, encoding, "medium", "decode_heuristic"

    return False, None, "medium", "undecodable_bytes"


def _file_type(path: Path, data: bytes, is_binary: bool) -> tuple[str, str, str, str]:
    """Return category, human-readable type, MIME type, and detection method."""
    for magic, category, label, mime_type in _MAGIC_TYPES:
        if data.startswith(magic):
            return category, label, mime_type, "magic"

    extension = path.suffix.casefold()
    if extension in _BINARY_EXTENSIONS:
        return "binary data", f"{extension[1:].upper()} binary file", "application/octet-stream", "extension"
    if extension in _EXTENSION_TYPES:
        return (*_EXTENSION_TYPES[extension], "extension")

    guessed_mime, _ = mimetypes.guess_type(path.name)
    if guessed_mime:
        category = "text" if guessed_mime.startswith("text/") else "binary data"
        return category, guessed_mime, guessed_mime, "mimetypes"
    if is_binary:
        return "binary data", "Unknown binary file", "application/octet-stream", "fallback"
    return "text", "Plain text", "text/plain", "fallback"


def _read_info_sample(path: Path) -> tuple[bytes, bool]:
    with path.open("rb") as file:
        data = file.read(MAX_INFO_SCAN_BYTES + 1)
    truncated = len(data) > MAX_INFO_SCAN_BYTES
    return data[:MAX_INFO_SCAN_BYTES], truncated


class GetFileInfoTool(Tool):
    name: ClassVar[str] = "get_file_info"
    description: ClassVar[str] = (
        "Return metadata, file type, MIME type, and best-effort encoding details "
        "for one existing local workspace file or directory."
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
        sample = b""
        scan_truncated = False
        is_binary = False
        encoding: str | None = None
        encoding_confidence = "high"
        encoding_detection = "directory"
        if is_file:
            sample, scan_truncated = _read_info_sample(path)
            is_text, encoding, encoding_confidence, encoding_detection = _detect_encoding(sample)
            is_binary = not is_text
        if is_file:
            type_category, type_label, mime_type, type_detection = _file_type(
                path, sample, is_binary
            )
            if type_category in {"image", "archive", "executable", "database", "binary data"}:
                is_binary = True
                encoding = None
                encoding_confidence = "high"
                encoding_detection = "known_binary_type"
        else:
            type_category, type_label, mime_type, type_detection = (
                "directory",
                "Directory",
                "inode/directory",
                "directory",
            )
        line_count: int | None = None
        line_count_truncated = scan_truncated

        if is_file and not is_binary and encoding is not None:
            decoded = _decode_strict(sample, encoding)
            if decoded is not None:
                line_count = decoded.count("\n")
                if not scan_truncated and sample and not decoded.endswith(("\n", "\r")):
                    line_count += 1

        return _success(
            self,
            {
                "path": _relative_path(context.workspace.root, path),
                "kind": "file" if is_file else "directory",
                "type_category": type_category,
                "file_type": type_label if is_file else "Directory",
                "mime_type": mime_type if is_file else "inode/directory",
                "type_detection": type_detection if is_file else "directory",
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "is_binary": is_binary,
                "encoding": encoding,
                "encoding_confidence": encoding_confidence,
                "encoding_detection": encoding_detection,
                "line_count": line_count,
                "line_count_truncated": line_count_truncated,
                "scan_truncated": scan_truncated,
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
