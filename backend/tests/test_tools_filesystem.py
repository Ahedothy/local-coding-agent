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
from coding_agent.tools.file_version import sha256_file


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
    assert result.output["sha256"] == sha256_file(tmp_path / "main.py")


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


def test_write_file_rejects_stale_expected_sha256_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("old\n", encoding="utf-8")
    original = path.read_bytes()
    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-stale",
                name="write_file",
                arguments={
                    "path": "main.py",
                    "content": "new\n",
                    "expected_sha256": "0" * 64,
                },
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert "stale file" in result.error
    assert path.read_bytes() == original


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
    assert result.output["structured_matches"][0]["path"] == "one.py"
    assert result.output["match_count"] == 1
    assert result.output["truncated"] is True


def test_search_files_supports_regex_case_and_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "before\n"
        "Value = 123\n"
        "after\n"
        "value = abc\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "notes.txt").write_text("Value = 456\n", encoding="utf-8")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="search_files",
                arguments={
                    "path": "src",
                    "glob": "*.py",
                    "query": r"value\s*=\s*\d+",
                    "use_regex": True,
                    "case_sensitive": False,
                    "context_lines": 1,
                },
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.output["path"] == "src"
    assert result.output["glob"] == "*.py"
    assert result.output["use_regex"] is True
    assert result.output["case_sensitive"] is False
    assert result.output["matches"] == ["src/main.py:2: Value = 123"]
    assert result.output["structured_matches"] == [
        {
            "path": "src/main.py",
            "line_number": 2,
            "line": "Value = 123",
            "match_start": 0,
            "match_end": 11,
            "line_truncated": False,
            "before_context": [
                {"line_number": 1, "line": "before", "line_truncated": False}
            ],
            "after_context": [
                {"line_number": 3, "line": "after", "line_truncated": False}
            ],
        }
    ]


def test_search_files_can_match_literal_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("Needle\nneedle\n", encoding="utf-8")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="search_files",
                arguments={
                    "query": "needle",
                    "case_sensitive": False,
                    "max_matches": 10,
                },
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is True
    assert result.output["matches"] == [
        "main.py:1: Needle",
        "main.py:2: needle",
    ]


def test_search_files_rejects_invalid_regex(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n", encoding="utf-8")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="search_files",
                arguments={"query": "(", "use_regex": True},
            ),
            make_context(tmp_path),
        )
    )

    assert result.success is False
    assert "invalid regular expression" in result.error


def test_search_files_truncates_long_matching_lines_around_the_match(
    tmp_path: Path,
) -> None:
    long_line = "a" * 700 + "needle" + "b" * 700
    (tmp_path / "main.py").write_text(f"{long_line}\n", encoding="utf-8")

    result = asyncio.run(
        filesystem_executor().execute(
            ToolCall(
                id="call-1",
                name="search_files",
                arguments={"query": "needle"},
            ),
            make_context(tmp_path),
        )
    )

    match = result.output["structured_matches"][0]
    assert result.success is True
    assert match["line_truncated"] is True
    assert len(match["line"]) <= 506
    assert "needle" in match["line"]
    assert match["match_start"] is not None
    assert match["line"][match["match_start"] : match["match_end"]] == "needle"
