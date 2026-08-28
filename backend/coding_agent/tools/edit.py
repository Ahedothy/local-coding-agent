"""Small, deterministic text editing tool for local workspace files."""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_PATCH_CHARS = 512 * 1024


class ReplaceInFileArguments(BaseModel):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(default=1, gt=0, le=100)


class ApplyPatchArguments(BaseModel):
    patch: str = Field(
        min_length=1,
        max_length=MAX_PATCH_CHARS,
        description=(
            "Standard unified diff text containing diff --git, ---/+++ file "
            "headers, and @@ hunk headers."
        ),
    )


def _is_binary(path: Path) -> bool:
    with path.open("rb") as file:
        return b"\x00" in file.read(8_192)


def _relative_path(workspace_root: Path, path: Path) -> str:
    return path.relative_to(workspace_root).as_posix() or "."


@dataclass(frozen=True, slots=True)
class _PatchPlan:
    path: Path
    relative_path: str
    original: bytes
    updated: bytes
    hunks_applied: int
    lines_added: int
    lines_removed: int


@dataclass(frozen=True, slots=True)
class _UnifiedHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]
    old_no_newline: bool = False
    new_no_newline: bool = False


@dataclass(frozen=True, slots=True)
class _UnifiedFilePatch:
    path: str
    hunks: tuple[_UnifiedHunk, ...]


def _patch_path(header: str) -> str:
    value = header[4:].split("\t", 1)[0].strip()
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    if not value or value == "/dev/null":
        raise ValueError("new and deleted files are not supported by apply_patch")
    return value.replace("\\", "/")


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    match = re.match(
        r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$",
        line,
    )
    if match is None:
        raise ValueError(f"invalid unified diff hunk header: {line}")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    new_start = int(match.group(3))
    new_count = int(match.group(4) or "1")
    return old_start, old_count, new_start, new_count


def _parse_unified_diff(patch: str) -> list[_UnifiedFilePatch]:
    """Parse standard unified diff text without delegating to a shell tool."""
    lines = patch.splitlines()
    file_patches: list[_UnifiedFilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_header = lines[index]
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("unified diff is missing its +++ file header")
        new_header = lines[index]
        index += 1
        old_path = _patch_path(old_header)
        new_path = _patch_path(new_header)
        if old_path != new_path:
            raise ValueError("renames are not supported by apply_patch")

        hunks: list[_UnifiedHunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if lines[index].startswith("diff --git ") or lines[index].startswith("index "):
                index += 1
                continue
            if not lines[index].startswith("@@ "):
                index += 1
                continue
            old_start, old_count, new_start, new_count = _parse_hunk_header(lines[index])
            index += 1
            hunk_lines: list[tuple[str, str]] = []
            old_actual = 0
            new_actual = 0
            old_no_newline = False
            new_no_newline = False
            while index < len(lines) and (old_actual < old_count or new_actual < new_count):
                line = lines[index]
                index += 1
                if line.startswith("\\ No newline at end of file"):
                    if not hunk_lines:
                        raise ValueError("newline marker cannot appear before a hunk line")
                    previous_kind = hunk_lines[-1][0]
                    if previous_kind in {" ", "-"}:
                        old_no_newline = True
                    if previous_kind in {" ", "+"}:
                        new_no_newline = True
                    continue
                if not line or line[0] not in {" ", "+", "-"}:
                    raise ValueError(f"invalid unified diff hunk line: {line}")
                kind = line[0]
                if kind in {" ", "-"}:
                    old_actual += 1
                if kind in {" ", "+"}:
                    new_actual += 1
                if old_actual > old_count or new_actual > new_count:
                    raise ValueError("unified diff hunk contains too many lines")
                hunk_lines.append((kind, line[1:]))
            if old_actual != old_count or new_actual != new_count:
                raise ValueError(
                    "unified diff hunk line count mismatch: "
                    f"expected {old_count}/{new_count}, found {old_actual}/{new_actual}"
                )
            hunks.append(
                _UnifiedHunk(
                    old_start,
                    old_count,
                    new_start,
                    new_count,
                    tuple(hunk_lines),
                    old_no_newline,
                    new_no_newline,
                )
            )
        if not hunks:
            raise ValueError(f"unified diff contains no hunks for {old_path}")
        file_patches.append(_UnifiedFilePatch(old_path, tuple(hunks)))

    if not file_patches:
        raise ValueError("patch does not contain unified diff file headers")
    normalized_paths = [item.path.casefold() for item in file_patches]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValueError("unified diff must not contain duplicate file paths")
    return file_patches


def _apply_unified_hunks(content: str, hunks: tuple[_UnifiedHunk, ...]) -> str:
    newline = "\r\n" if "\r\n" in content else "\n"
    had_final_newline = content.endswith(("\n", "\r"))
    updated_final_newline = had_final_newline
    current_lines = content.splitlines()
    offset = 0
    for hunk in hunks:
        start = (0 if hunk.old_start == 0 else hunk.old_start - 1) + offset
        expected = [text for kind, text in hunk.lines if kind in {" ", "-"}]
        replacement = [text for kind, text in hunk.lines if kind in {" ", "+"}]
        if start < 0 or current_lines[start : start + len(expected)] != expected:
            raise ValueError(
                f"patch context mismatch near old line {hunk.old_start}"
            )
        current_lines[start : start + len(expected)] = replacement
        offset += hunk.new_count - hunk.old_count
        if hunk.new_no_newline:
            updated_final_newline = False
        elif hunk.old_no_newline and hunk.new_count > 0:
            updated_final_newline = True
    updated = newline.join(current_lines)
    if updated_final_newline and current_lines:
        updated += newline
    return updated


def _build_unified_diff(plans: list[_PatchPlan]) -> str:
    """Render the validated changes as a standard multi-file unified diff."""
    output: list[str] = []
    for plan in plans:
        original_lines = plan.original.decode("utf-8").splitlines()
        updated_lines = plan.updated.decode("utf-8").splitlines()
        diff_lines = list(
            difflib.unified_diff(
                original_lines,
                updated_lines,
                fromfile=f"a/{plan.relative_path}",
                tofile=f"b/{plan.relative_path}",
                lineterm="",
            )
        )
        if not diff_lines:
            continue
        output.append(f"diff --git a/{plan.relative_path} b/{plan.relative_path}")
        output.extend(diff_lines)
    return "\n".join(output) + ("\n" if output else "")


def _line_delta(original: str, updated: str) -> tuple[int, int]:
    """Count changed lines without exposing a full diff to the model."""
    original_lines = original.splitlines()
    updated_lines = updated.splitlines()
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(None, original_lines, updated_lines, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += old_end - old_start
        if tag in {"replace", "insert"}:
            added += new_end - new_start
    return added, removed


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace one file through a same-directory temporary file."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


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

        original = path.read_bytes()
        content = original.decode("utf-8")
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
        updated_bytes = updated.encode("utf-8")
        _atomic_write_bytes(path, updated_bytes)
        lines_added, lines_removed = _line_delta(content, updated)
        diff = _build_unified_diff(
            [
                _PatchPlan(
                    path=path,
                    relative_path=_relative_path(context.workspace.root, path),
                    original=original,
                    updated=updated_bytes,
                    hunks_applied=1,
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                )
            ]
        )

        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "path": _relative_path(context.workspace.root, path),
                "replacements": replacement_count,
                "bytes_written": len(updated_bytes),
                "diff": diff,
            },
        )


class ApplyPatchTool(Tool):
    name: ClassVar[str] = "apply_patch"
    description: ClassVar[str] = (
        "Apply a standard multi-file unified diff to existing UTF-8 files "
        "inside the workspace. Validate all hunks before writing and roll back "
        "if any file write fails. Use replace_in_file for one simple replacement."
    )
    parameters_model: ClassVar[type[BaseModel]] = ApplyPatchArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ApplyPatchArguments,
    ) -> ToolResult:
        try:
            file_patches = _parse_unified_diff(arguments.patch)
        except Exception as exc:
            return self._failed_result(f"invalid unified diff: {exc}", [])

        plans: list[_PatchPlan] = []
        applied_hunks: list[dict[str, object]] = []
        rejected_hunks: list[dict[str, object]] = []

        for file_patch in file_patches:
            try:
                path = context.workspace.ensure_readable_file(file_patch.path)
                if _is_binary(path):
                    raise ValueError(f"binary file is not supported: {file_patch.path}")
                if path.stat().st_size > MAX_EDIT_FILE_BYTES:
                    raise ValueError(f"file is too large to edit: {file_patch.path}")
                original = path.read_bytes()
                content = original.decode("utf-8")
            except Exception as exc:
                rejected_hunks.append({"path": file_patch.path, "reason": str(exc)})
                return self._failed_result(
                    str(exc), rejected_hunks
                )

            relative_path = _relative_path(context.workspace.root, path)
            try:
                updated_content = _apply_unified_hunks(content, file_patch.hunks)
            except Exception as exc:
                rejected_hunks.append({"path": relative_path, "reason": str(exc)})
                return self._failed_result(
                    f"patch context mismatch in {relative_path}: {exc}",
                    rejected_hunks,
                )

            updated = updated_content.encode("utf-8")
            if len(updated) > MAX_EDIT_FILE_BYTES:
                reason = f"patched file is too large to edit: {file_patch.path}"
                rejected_hunks.append({"path": relative_path, "reason": reason})
                return self._failed_result(reason, rejected_hunks)
            lines_added, lines_removed = _line_delta(
                original.decode("utf-8"), updated_content
            )
            applied_hunks.extend(
                {"path": relative_path, "hunk": index}
                for index, _ in enumerate(file_patch.hunks, start=1)
            )
            plans.append(
                _PatchPlan(
                    path=path,
                    relative_path=relative_path,
                    original=original,
                    updated=updated,
                    hunks_applied=len(file_patch.hunks),
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                )
            )

        touched_files = [
            {
                "path": plan.relative_path,
                "hunks_applied": plan.hunks_applied,
                "lines_added": plan.lines_added,
                "lines_removed": plan.lines_removed,
                "bytes_before": len(plan.original),
                "bytes_after": len(plan.updated),
            }
            for plan in plans
        ]
        diff_summary = {
            "files": len(plans),
            "hunks": len(applied_hunks),
            "lines_added": sum(plan.lines_added for plan in plans),
            "lines_removed": sum(plan.lines_removed for plan in plans),
            "bytes_before": sum(len(plan.original) for plan in plans),
            "bytes_after": sum(len(plan.updated) for plan in plans),
        }
        unified_diff = _build_unified_diff(plans)

        written: list[_PatchPlan] = []
        try:
            for plan in plans:
                _atomic_write_bytes(plan.path, plan.updated)
                written.append(plan)
        except Exception as exc:
            rollback_error: Exception | None = None
            for plan in reversed(written):
                try:
                    _atomic_write_bytes(plan.path, plan.original)
                except Exception as restore_exc:
                    rollback_error = restore_exc
                    break
            error = f"patch write failed and changes were rolled back: {exc}"
            if rollback_error is not None:
                error += f"; rollback failed: {rollback_error}"
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=False,
                error=error,
                output={
                    "touched_files": [],
                    "applied_hunks": [],
                    "rejected_hunks": rejected_hunks,
                    "diff_summary": diff_summary,
                    "diff": unified_diff,
                    "rolled_back": True,
                },
            )

        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "touched_files": touched_files,
                "applied_hunks": applied_hunks,
                "rejected_hunks": rejected_hunks,
                "diff_summary": diff_summary,
                "diff": unified_diff,
                "rolled_back": False,
            },
        )

    def _failed_result(
        self,
        error: str,
        rejected_hunks: list[dict[str, object]],
    ) -> ToolResult:
        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=False,
            error=error,
            output={
                "touched_files": [],
                "applied_hunks": [],
                "rejected_hunks": rejected_hunks,
                "diff_summary": {
                    "files": 0,
                    "hunks": 0,
                    "lines_added": 0,
                    "lines_removed": 0,
                    "bytes_before": 0,
                    "bytes_after": 0,
                },
                "diff": "",
                "rolled_back": False,
            },
        )


EDIT_TOOLS: tuple[Tool, ...] = (ReplaceInFileTool(), ApplyPatchTool())
