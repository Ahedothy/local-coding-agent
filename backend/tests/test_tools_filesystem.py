from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.filesystem import (
    FILESYSTEM_TOOLS,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def filesystem_executor() -> ToolExecutor:
    return ToolExecutor(ToolRegistry(FILESYSTEM_TOOLS))


def test_filesystem_tools_are_registered_with_expected_contracts() -> None:
    assert [tool.name for tool in FILESYSTEM_TOOLS] == [
        "list_files",
        "read_file",
        "write_file",
        "search_files",
    ]
    assert isinstance(FILESYSTEM_TOOLS[0], ListFilesTool)
    assert isinstance(FILESYSTEM_TOOLS[1], ReadFileTool)
    assert isinstance(FILESYSTEM_TOOLS[2], WriteFileTool)
    assert isinstance(FILESYSTEM_TOOLS[3], SearchFilesTool)


def test_list_files_lists_files_and_skips_ignored_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.pyc").write_bytes(b"cache")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(id="call-1", name="list_files", arguments={"recursive": True}),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.output["files"] == ["src/main.py"]


def test_read_file_supports_line_ranges_and_truncation(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_bytes(b"one\ntwo\nthree\n")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="read_file",
                arguments={"path": "main.py", "start_line": 2, "end_line": 3},
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.output["content"] == "two\nthree\n"
    assert result.output["truncated"] is False


def test_read_file_rejects_binary_and_workspace_escape(tmp_path: Path) -> None:
    (tmp_path / "image.bin").write_bytes(b"\x00\x01\x02")
    executor = filesystem_executor()

    binary_result = asyncio.run(
        executor.execute(
            ToolCall(id="call-1", name="read_file", arguments={"path": "image.bin"}),
            make_context(tmp_path),
        )
    )
    escape_result = asyncio.run(
        executor.execute(
            ToolCall(id="call-2", name="read_file", arguments={"path": "../secret.txt"}),
            make_context(tmp_path),
        )
    )

    assert binary_result.success is False
    assert "binary file" in binary_result.error
    assert escape_result.success is False
    assert "outside the workspace" in escape_result.error


def test_write_file_creates_and_protects_files(tmp_path: Path) -> None:
    executor = filesystem_executor()
    context = make_context(tmp_path)

    created = asyncio.run(
        executor.execute(
            ToolCall(
                id="call-1",
                name="write_file",
                arguments={
                    "path": "src/main.py",
                    "content": "print('ok')",
                    "create_parent_dirs": True,
                },
            ),
            context,
        )
    )
    protected = asyncio.run(
        executor.execute(
            ToolCall(
                id="call-2",
                name="write_file",
                arguments={"path": "src/main.py", "content": "changed", "overwrite": False},
            ),
            context,
        )
    )

    assert created.success is True
    assert (tmp_path / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')"
    assert protected.success is False
    assert "already exists" in protected.error


def test_write_file_rejects_workspace_escape(tmp_path: Path) -> None:
    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="write_file",
                arguments={"path": "../outside.txt", "content": "unsafe"},
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert "outside the workspace" in result.error


def test_search_files_finds_text_and_respects_match_limit(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("needle\nother\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle again\n", encoding="utf-8")
    (tmp_path / "ignored.pyc").write_bytes(b"\x00needle")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="search_files",
                arguments={"query": "needle", "max_matches": 1},
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0].endswith(":1: needle")
    assert result.output["truncated"] is True
