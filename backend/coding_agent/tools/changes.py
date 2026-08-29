"""Local file snapshots used for safe edit undo and unified diffs."""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from coding_agent.workspace import Workspace

from .base import ToolContext, ToolResult


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    relative_path: str
    existed: bool
    content: bytes | None


@dataclass(slots=True)
class ChangeRecord:
    change_id: str
    session_id: str
    tool_name: str
    snapshots: tuple[FileSnapshot, ...]
    after: tuple[FileSnapshot, ...]
    diff: str
    turn_id: str | None = None
    reverted: bool = False


def _atomic_write(path: Path, content: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".undo.tmp", delete=False
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


def _decode_lines(content: bytes | None) -> list[str]:
    if content is None:
        return []
    return content.decode("utf-8").splitlines()


def _diff_for_file(before: FileSnapshot, after: FileSnapshot) -> str:
    if before.content == after.content and before.existed == after.existed:
        return ""
    from_name = f"a/{before.relative_path}" if before.existed else "/dev/null"
    to_name = f"b/{after.relative_path}" if after.existed else "/dev/null"
    lines = list(
        difflib.unified_diff(
            _decode_lines(before.content),
            _decode_lines(after.content),
            fromfile=from_name,
            tofile=to_name,
            lineterm="",
        )
    )
    if not lines:
        return ""
    return "\n".join([f"diff --git a/{before.relative_path} b/{after.relative_path}", *lines])


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value.startswith("b/"):
            value = value[2:]
        if value and value != "/dev/null":
            paths.append(value.replace("\\", "/"))
    return list(dict.fromkeys(paths))


def _edit_paths(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    if tool_name in {"write_file", "replace_in_file"}:
        path = arguments.get("path")
        return [str(path)] if path else []
    if tool_name == "apply_patch":
        return _patch_paths(str(arguments.get("patch", "")))
    return []


class ChangeStore:
    """Store per-session snapshots and restore them without clobbering new edits."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.records: dict[str, ChangeRecord] = {}
        self._groups: dict[tuple[str, str], str] = {}

    def capture(
        self,
        tool_name: str,
        arguments: Any,
        context: ToolContext,
    ) -> tuple[FileSnapshot, ...] | None:
        if tool_name not in {"write_file", "replace_in_file", "apply_patch"}:
            return None
        argument_data = arguments.model_dump(mode="python")
        if tool_name == "apply_patch" and argument_data.get("dry_run") is True:
            return None
        snapshots: list[FileSnapshot] = []
        for relative_path in _edit_paths(tool_name, argument_data):
            path = context.workspace.resolve_path(relative_path)
            if path.exists() and not path.is_file():
                raise ValueError(f"edit target is not a file: {relative_path}")
            snapshots.append(
                FileSnapshot(
                    path=path,
                    relative_path=relative_path.replace("\\", "/"),
                    existed=path.exists(),
                    content=path.read_bytes() if path.exists() else None,
                )
            )
        return tuple(snapshots)

    def record(
        self,
        context: ToolContext,
        tool_name: str,
        before: tuple[FileSnapshot, ...] | None,
        result: ToolResult,
    ) -> ToolResult:
        if not before or not result.success:
            return result
        group_key = (context.session_id, context.turn_id) if context.turn_id else None
        change = self.records.get(self._groups.get(group_key, "")) if group_key else None
        if change is None:
            change = ChangeRecord(
                change_id=str(uuid4()),
                session_id=context.session_id,
                tool_name=tool_name,
                snapshots=before,
                after=(),
                diff="",
                turn_id=context.turn_id,
            )
            self.records[change.change_id] = change
            if group_key:
                self._groups[group_key] = change.change_id

        snapshots_by_path = {snapshot.path: snapshot for snapshot in change.snapshots}
        for snapshot in before:
            snapshots_by_path.setdefault(snapshot.path, snapshot)
        merged_before = tuple(snapshots_by_path.values())
        after = tuple(
            FileSnapshot(
                path=snapshot.path,
                relative_path=snapshot.relative_path,
                existed=snapshot.path.exists(),
                content=snapshot.path.read_bytes() if snapshot.path.exists() else None,
            )
            for snapshot in merged_before
        )
        diff = "\n".join(
            value
            for value in (_diff_for_file(old, new) for old, new in zip(merged_before, after))
            if value
        )
        change.snapshots = merged_before
        change.after = after
        change.diff = f"{diff}\n" if diff else ""
        output = result.output if isinstance(result.output, dict) else {}
        return result.model_copy(
            update={
                "output": {**output, "diff": change.diff},
                "metadata": {**result.metadata, "change_id": change.change_id},
            }
        )

    def get(self, change_id: str) -> ChangeRecord | None:
        return self.records.get(change_id)

    def revert(self, change_id: str) -> ChangeRecord:
        record = self.records.get(change_id)
        if record is None:
            raise KeyError("change not found")
        if record.reverted:
            return record
        for snapshot in record.after:
            current_exists = snapshot.path.exists()
            current_content = snapshot.path.read_bytes() if current_exists else None
            if current_exists != snapshot.existed or current_content != snapshot.content:
                raise ValueError(
                    f"cannot undo {snapshot.relative_path}: file changed after the Agent edit"
                )
        for snapshot in reversed(record.snapshots):
            if snapshot.existed:
                if snapshot.content is None:
                    raise ValueError(f"missing snapshot content: {snapshot.relative_path}")
                _atomic_write(snapshot.path, snapshot.content)
            elif snapshot.path.exists():
                snapshot.path.unlink()
        record.reverted = True
        return record
