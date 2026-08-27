"""Small, deterministic text editing tool for local workspace files."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


MAX_EDIT_FILE_BYTES = 1024 * 1024


class ReplaceInFileArguments(BaseModel):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(default=1, gt=0, le=100)


def _is_binary(path: Path) -> bool:
    with path.open("rb") as file:
        return b"\x00" in file.read(8_192)


def _relative_path(workspace_root: Path, path: Path) -> str:
    return path.relative_to(workspace_root).as_posix() or "."


class ReplaceInFileTool(Tool):
    name: ClassVar[str] = "replace_in_file"
    description: ClassVar[str] = "Replace exact text in one UTF-8 file inside the workspace."
    parameters_model: ClassVar[type[BaseModel]] = ReplaceInFileArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ReplaceInFileArguments,
    ) -> ToolResult:
        path = context.workspace.ensure_readable_file(arguments.path)
        if _is_binary(path):
            raise ValueError(f"binary file is not supported: {arguments.path}")
        if path.stat().st_size > MAX_EDIT_FILE_BYTES:
            raise ValueError(f"file is too large to edit: {arguments.path}")

        content = path.read_bytes().decode("utf-8")
        replacement_count = content.count(arguments.old_text)
        if replacement_count != arguments.expected_replacements:
            raise ValueError(
                "replacement count mismatch: "
                f"expected {arguments.expected_replacements}, found {replacement_count}"
            )

        updated = content.replace(
            arguments.old_text,
            arguments.new_text,
            arguments.expected_replacements,
        )
        path.write_bytes(updated.encode("utf-8"))

        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "path": _relative_path(context.workspace.root, path),
                "replacements": replacement_count,
                "bytes_written": len(updated.encode("utf-8")),
            },
        )


EDIT_TOOLS: tuple[Tool, ...] = (ReplaceInFileTool(),)
