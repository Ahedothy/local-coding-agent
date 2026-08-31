"""Small, deterministic text editing tool for local workspace files."""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import json

from pydantic import BaseModel, Field, field_validator

from .base import Tool, ToolContext, ToolResult
from .file_version import ensure_expected_sha256, sha256_bytes


MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_PATCH_CHARS = 512 * 1024


class ReplaceInFileArguments(BaseModel):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(default=1, gt=0, le=100)
    expected_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 returned by a previous read of this file.",
    )


class ApplyPatchArguments(BaseModel):
    patch: str = Field(
        min_length=1,
        max_length=MAX_PATCH_CHARS,
        description=(
            "Standard unified diff text containing diff --git, ---/+++ file "
            "headers, and @@ hunk headers."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, parse and validate the patch and return the generated "
            "diff without writing files. Use this to diagnose uncertain patches."
        ),
    )
    expected_sha256: dict[str, str] = Field(
        default_factory=dict,
        description="Optional path-to-SHA-256 map returned by previous read_file calls.",
    )

    @field_validator("expected_sha256", mode="before")
    @classmethod
    def decode_expected_sha256(cls, value: object) -> object:
        """Accept JSON-encoded maps emitted by some tool-calling models."""
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("expected_sha256 must be a JSON object") from exc
            if not isinstance(decoded, dict):
                raise ValueError("expected_sha256 must be a JSON object")
            return decoded
        return value


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
    declared_old_count: int | None = None
    declared_new_count: int | None = None
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


def _starts_file_header(lines: list[str], index: int) -> bool:
    return (
        lines[index].startswith("--- ")
        and index + 1 < len(lines)
        and lines[index + 1].startswith("+++ ")
    )


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
            old_start, declared_old_count, new_start, declared_new_count = (
                _parse_hunk_header(lines[index])
            )
            index += 1
            hunk_lines: list[tuple[str, str]] = []
            old_actual = 0
            new_actual = 0
            old_no_newline = False
            new_no_newline = False
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or _starts_file_header(lines, index):
                    break
                if line.startswith("diff --git ") or line.startswith("index "):
                    break
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
                hunk_lines.append((kind, line[1:]))

            if not hunk_lines:
                raise ValueError("unified diff hunk contains no changed or context lines")
            if old_actual == 0 and new_actual == 0:
                raise ValueError("unified diff hunk contains no effective lines")
            if old_actual != declared_old_count or new_actual != declared_new_count:
                # Header counts are redundant with the body. Normalize this common
                # model error, then still require exact file context before writing.
                old_count = old_actual
                new_count = new_actual
            else:
                old_count = declared_old_count
                new_count = declared_new_count
            if old_count == 0 and new_count == 0:
                raise ValueError(
                    "unified diff hunk line count mismatch: "
                    f"expected {declared_old_count}/{declared_new_count}, "
                    f"found {old_actual}/{new_actual}; "
                    "old count must equal context plus removed lines and new count "
                    "must equal context plus added lines"
                )
            hunks.append(
                _UnifiedHunk(
                    old_start,
                    old_count,
                    new_start,
                    new_count,
                    tuple(hunk_lines),
                    declared_old_count,
                    declared_new_count,
                    old_no_newline,
                    new_no_newline,
                )
            )
        if not hunks:
            raise ValueError(
                f"unified diff contains no hunks for {old_path}; include at least one "
                "@@ hunk with context, removed, or added lines"
            )
        file_patches.append(_UnifiedFilePatch(old_path, tuple(hunks)))

    if not file_patches:
        raise ValueError("patch does not contain unified diff file headers")
    normalized_paths = [item.path.casefold() for item in file_patches]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValueError("unified diff must not contain duplicate file paths")
    return file_patches


def _find_hunk_start(
    current_lines: list[str],
    expected: list[str],
    suggested_start: int,
    minimum_start: int,
    old_start: int,
) -> int:
    """Locate a hunk by exact context, tolerating only line-number drift."""
    bounded_suggested = max(minimum_start, suggested_start)
    if not expected:
        if bounded_suggested <= len(current_lines):
            return bounded_suggested
        raise ValueError(f"patch insertion point is outside the file near old line {old_start}")
    if (
        bounded_suggested + len(expected) <= len(current_lines)
        and current_lines[bounded_suggested : bounded_suggested + len(expected)]
        == expected
    ):
        return bounded_suggested

    candidates = [
        index
        for index in range(minimum_start, len(current_lines) - len(expected) + 1)
        if current_lines[index : index + len(expected)] == expected
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(
            f"patch context is ambiguous near old line {old_start}; "
            f"exact hunk context appears {len(candidates)} times"
        )
    raise ValueError(f"patch context mismatch near old line {old_start}")


def _apply_unified_hunks(content: str, hunks: tuple[_UnifiedHunk, ...]) -> str:
    newline = "\r\n" if "\r\n" in content else "\n"
    had_final_newline = content.endswith(("\n", "\r"))
    updated_final_newline = had_final_newline
    current_lines = content.splitlines()
    offset = 0
    minimum_start = 0
    previous_old_start = 0
    for hunk in hunks:
        expected = [text for kind, text in hunk.lines if kind in {" ", "-"}]
        replacement = [text for kind, text in hunk.lines if kind in {" ", "+"}]
        if hunk.old_start and hunk.old_start < previous_old_start:
            raise ValueError(
                "patch hunks must be ordered by original file line number"
            )
        suggested_start = (0 if hunk.old_start == 0 else hunk.old_start - 1) + offset
        start = _find_hunk_start(
            current_lines,
            expected,
            suggested_start,
            minimum_start,
            hunk.old_start,
        )
        current_lines[start : start + len(expected)] = replacement
        offset += hunk.new_count - hunk.old_count
        minimum_start = start + len(replacement)
        previous_old_start = hunk.old_start
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


def _newline_variants(value: str) -> tuple[str, ...]:
    """Return equivalent LF/CRLF forms without changing literal backslash-n text."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(dict.fromkeys((value, normalized, normalized.replace("\n", "\r\n"))))


def _replace_text_with_newline_compatibility(
    content: str,
    old_text: str,
    new_text: str,
    expected_replacements: int,
) -> tuple[str, int] | None:
    """Match LF/CRLF equivalents while preserving the matched file style."""
    for candidate in _newline_variants(old_text):
        replacement_count = content.count(candidate)
        if replacement_count != expected_replacements:
            continue
        newline = "\r\n" if "\r\n" in candidate else "\n"
        replacement = new_text.replace("\r\n", "\n").replace("\r", "\n")
        replacement = replacement.replace("\n", newline)
        return content.replace(candidate, replacement, expected_replacements), replacement_count
    return None


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
    description: ClassVar[str] = (
        "Replace one small, contiguous exact text match in one UTF-8 file. "
        "Use only for a single simple replacement, usually one line or a short "
        "snippet; use apply_patch for multi-line, multi-location, structural, "
        "or multi-file edits."
    )
    parameters_model: ClassVar[type[BaseModel]] = ReplaceInFileArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: ReplaceInFileArguments,
    ) -> ToolResult:
        path = context.workspace.ensure_readable_file(arguments.path)
        ensure_expected_sha256(path, arguments.expected_sha256, arguments.path)
        if _is_binary(path):
            raise ValueError(f"binary file is not supported: {arguments.path}")
        if path.stat().st_size > MAX_EDIT_FILE_BYTES:
            raise ValueError(f"file is too large to edit: {arguments.path}")

        original = path.read_bytes()
        content = original.decode("utf-8")
        replacement = _replace_text_with_newline_compatibility(
            content,
            arguments.old_text,
            arguments.new_text,
            arguments.expected_replacements,
        )
        if replacement is None:
            replacement_count = content.count(arguments.old_text)
            raise ValueError(
                "replacement count mismatch: "
                f"expected {arguments.expected_replacements}, found {replacement_count}"
            )
        updated, replacement_count = replacement
        updated_bytes = updated.encode("utf-8")
        ensure_expected_sha256(path, arguments.expected_sha256, arguments.path)
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
                "sha256": sha256_bytes(updated_bytes),
            },
        )


class ApplyPatchTool(Tool):
    name: ClassVar[str] = "apply_patch"
    description: ClassVar[str] = (
        "Preferred editing tool for multi-line, multi-location, structural, or "
        "multi-file changes. Apply a standard unified diff to existing UTF-8 "
        "files inside the workspace. Every changed region must be represented "
        "by a complete @@ hunk with matching context. Hunks should be ordered "
        "top to bottom using original file line numbers; the executor can recover "
        "a unique exact context when a line number drifts, but it never ignores "
        "or fuzzes source text. Redundant hunk line counts are normalized from "
        "the body before context validation. "
        "Set dry_run=true to validate a patch and inspect the generated diff "
        "without writing files; if dry_run succeeds, resend the same patch with "
        "dry_run=false to apply it. "
        "Validate all hunks before writing and roll back if any file write "
        "fails. Use replace_in_file only for one small simple exact replacement."
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
        normalized_hunks: list[dict[str, object]] = []
        rejected_hunks: list[dict[str, object]] = []

        for file_patch in file_patches:
            try:
                path = context.workspace.ensure_readable_file(file_patch.path)
                if _is_binary(path):
                    raise ValueError(f"binary file is not supported: {file_patch.path}")
                if path.stat().st_size > MAX_EDIT_FILE_BYTES:
                    raise ValueError(f"file is too large to edit: {file_patch.path}")
                original = path.read_bytes()
                relative_path = _relative_path(context.workspace.root, path)
                ensure_expected_sha256(
                    path,
                    arguments.expected_sha256.get(relative_path),
                    relative_path,
                )
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
            normalized_hunks.extend(
                {
                    "path": relative_path,
                    "hunk": index,
                    "declared_old_count": hunk.declared_old_count,
                    "declared_new_count": hunk.declared_new_count,
                    "old_count": hunk.old_count,
                    "new_count": hunk.new_count,
                }
                for index, hunk in enumerate(file_patch.hunks, start=1)
                if hunk.declared_old_count != hunk.old_count
                or hunk.declared_new_count != hunk.new_count
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

        if arguments.dry_run:
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=True,
                output={
                    "dry_run": True,
                    "would_write": False,
                    "touched_files": touched_files,
                    "applied_hunks": applied_hunks,
                    "normalized_hunks": normalized_hunks,
                    "rejected_hunks": rejected_hunks,
                    "diff_summary": diff_summary,
                    "diff": unified_diff,
                    "rolled_back": False,
                },
            )

        written: list[_PatchPlan] = []
        try:
            for plan in plans:
                ensure_expected_sha256(
                    plan.path,
                    arguments.expected_sha256.get(plan.relative_path),
                    plan.relative_path,
                )
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
                    "dry_run": False,
                    "would_write": True,
                    "touched_files": [],
                    "applied_hunks": [],
                    "normalized_hunks": normalized_hunks,
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
                "dry_run": False,
                "would_write": True,
                "touched_files": touched_files,
                "applied_hunks": applied_hunks,
                "normalized_hunks": normalized_hunks,
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
                "dry_run": False,
                "would_write": False,
                "touched_files": [],
                "applied_hunks": [],
                "normalized_hunks": [],
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


EDIT_TOOLS: tuple[Tool, ...] = (ApplyPatchTool(), ReplaceInFileTool())
