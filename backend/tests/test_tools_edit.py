from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def execute_edit(tmp_path: Path, arguments: dict) -> object:
    executor = ToolExecutor(ToolRegistry(EDIT_TOOLS))
    return asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="replace_in_file", arguments=arguments),
            make_context(tmp_path),
        )
    )


def test_replace_in_file_replaces_one_exact_match(tmp_path: Path) -> None:
    path = tmp_path / "calculator.py"
    path.write_bytes(b"def add(a, b):\n    return a - b\n")

    result = execute_edit(
        tmp_path,
        {
            "path": "calculator.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
        },
    )

    assert result.success is True
    assert result.output["replacements"] == 1
    assert path.read_bytes() == b"def add(a, b):\n    return a + b\n"


def test_replace_in_file_fails_when_text_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    original = b"value = 1\n"
    path.write_bytes(original)

    result = execute_edit(
        tmp_path,
        {"path": "main.py", "old_text": "missing", "new_text": "changed"},
    )

    assert result.success is False
    assert "expected 1, found 0" in result.error
    assert path.read_bytes() == original


def test_replace_in_file_protects_against_unexpected_multiple_matches(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    original = b"value = 1\nvalue = 1\n"
    path.write_bytes(original)

    result = execute_edit(
        tmp_path,
        {"path": "main.py", "old_text": "value = 1", "new_text": "value = 2"},
    )

    assert result.success is False
    assert "expected 1, found 2" in result.error
    assert path.read_bytes() == original


def test_replace_in_file_honors_expected_replacement_count(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_bytes(b"value = 1\nvalue = 1\n")

    result = execute_edit(
        tmp_path,
        {
            "path": "main.py",
            "old_text": "value = 1",
            "new_text": "value = 2",
            "expected_replacements": 2,
        },
    )

    assert result.success is True
    assert result.output["replacements"] == 2
    assert path.read_bytes() == b"value = 2\nvalue = 2\n"


def test_replace_in_file_rejects_binary_files(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"before\x00after")

    result = execute_edit(
        tmp_path,
        {"path": "data.bin", "old_text": "before", "new_text": "changed"},
    )

    assert result.success is False
    assert "binary file" in result.error


def test_replace_in_file_rejects_workspace_escape(tmp_path: Path) -> None:
    result = execute_edit(
        tmp_path,
        {"path": "../outside.py", "old_text": "old", "new_text": "new"},
    )

    assert result.success is False
    assert "outside the workspace" in result.error
