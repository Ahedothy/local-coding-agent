from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools import inspection as inspection_tools
from coding_agent.tools.inspection import INSPECTION_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def execute(tmp_path: Path, name: str, arguments: dict) -> object:
    return asyncio.run(
        ToolExecutor(ToolRegistry(INSPECTION_TOOLS)).execute(
            ToolCall(id="call-1", name=name, arguments=arguments),
            make_context(tmp_path),
        )
    )


def test_inspection_tools_are_registered() -> None:
    assert [tool.name for tool in INSPECTION_TOOLS] == [
        "get_file_info",
        "list_directory_tree",
        "git_diff",
    ]


def test_get_file_info_reports_text_metadata(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_bytes(b"one\ntwo\n")

    result = execute(tmp_path, "get_file_info", {"path": "main.py"})

    assert result.success is True
    assert result.output["path"] == "main.py"
    assert result.output["kind"] == "file"
    assert result.output["size_bytes"] == len("one\ntwo\n".encode("utf-8"))
    assert result.output["is_binary"] is False
    assert result.output["encoding"] == "utf-8"
    assert result.output["encoding_confidence"] == "high"
    assert result.output["encoding_detection"] == "utf8"
    assert result.output["file_type"] == "Python source"
    assert result.output["mime_type"] == "text/x-python"
    assert result.output["type_detection"] == "extension"
    assert result.output["line_count"] == 2
    assert result.output["line_count_truncated"] is False
    assert result.output["modified_at"].endswith("+00:00")


def test_list_directory_tree_respects_entry_limit(tmp_path: Path) -> None:
    for name in ("one.py", "two.py", "three.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")

    result = execute(
        tmp_path,
        "list_directory_tree",
        {"max_depth": 1, "max_entries": 2},
    )

    assert result.success is True
    assert len(result.output["entries"]) == 2
    assert result.output["truncated"] is True


def test_get_file_info_handles_binary_and_directories(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"value\x00bytes")
    (tmp_path / "src").mkdir()

    binary = execute(tmp_path, "get_file_info", {"path": "data.bin"})
    directory = execute(tmp_path, "get_file_info", {"path": "src"})

    assert binary.success is True
    assert binary.output["is_binary"] is True
    assert binary.output["encoding"] is None
    assert binary.output["type_category"] == "binary data"
    assert binary.output["mime_type"] == "application/octet-stream"
    assert directory.output["kind"] == "directory"
    assert directory.output["type_category"] == "directory"
    assert directory.output["file_type"] == "Directory"
    assert directory.output["line_count"] is None


def test_get_file_info_detects_bom_encoding_and_line_count(tmp_path: Path) -> None:
    path = tmp_path / "message.txt"
    path.write_bytes("第一行\n第二行".encode("utf-16"))

    result = execute(tmp_path, "get_file_info", {"path": "message.txt"})

    assert result.success is True
    assert result.output["is_binary"] is False
    assert result.output["encoding"] == "utf-16-le"
    assert result.output["encoding_confidence"] == "high"
    assert result.output["encoding_detection"] == "bom"
    assert result.output["line_count"] == 2


def test_get_file_info_detects_common_legacy_text_encoding(tmp_path: Path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes("你好，世界\n".encode("gb18030"))

    result = execute(tmp_path, "get_file_info", {"path": "legacy.txt"})

    assert result.success is True
    assert result.output["is_binary"] is False
    assert result.output["encoding"] == "gb18030"
    assert result.output["encoding_confidence"] == "medium"
    assert result.output["encoding_detection"] == "decode_heuristic"


def test_get_file_info_uses_magic_bytes_for_binary_type(tmp_path: Path) -> None:
    path = tmp_path / "image.bin"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"plain-looking payload")

    result = execute(tmp_path, "get_file_info", {"path": "image.bin"})

    assert result.success is True
    assert result.output["is_binary"] is True
    assert result.output["file_type"] == "PNG image"
    assert result.output["mime_type"] == "image/png"
    assert result.output["type_detection"] == "magic"
    assert result.output["encoding"] is None


def test_get_file_info_marks_large_file_scan_as_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inspection_tools, "MAX_INFO_SCAN_BYTES", 4)
    path = tmp_path / "large.txt"
    path.write_bytes(b"one\ntwo\nthree\n")

    result = execute(tmp_path, "get_file_info", {"path": "large.txt"})

    assert result.success is True
    assert result.output["scan_truncated"] is True
    assert result.output["line_count_truncated"] is True


def test_list_directory_tree_is_compact_and_skips_ignored_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "src" / "nested" / "test.py").write_text("pass", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")

    result = execute(tmp_path, "list_directory_tree", {"max_depth": 3})

    assert result.success is True
    assert [entry["path"] for entry in result.output["entries"]] == [
        "src",
        "src/nested",
        "src/nested/test.py",
        "src/main.py",
    ]
    assert "node_modules" not in result.output["tree"]
    assert result.output["truncated"] is False


def test_list_directory_tree_reports_depth_truncation_and_escape(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "nested" / "main.py").write_text("pass", encoding="utf-8")

    shallow = execute(
        tmp_path,
        "list_directory_tree",
        {"path": ".", "max_depth": 1},
    )
    escaped = execute(
        tmp_path,
        "list_directory_tree",
        {"path": "../outside"},
    )

    assert shallow.success is True
    assert [entry["path"] for entry in shallow.output["entries"]] == ["src"]
    assert shallow.output["truncated"] is True
    assert escaped.success is False
    assert "outside the workspace" in escaped.error


def git_command(tmp_path: Path, *arguments: str) -> None:
    command = ["git", *arguments]
    if arguments and arguments[0] == "commit":
        command[1:1] = ["-c", "commit.gpgSign=false"]
    result = subprocess.run(
        command,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_diff_reports_tracked_and_untracked_workspace_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    git_command(tmp_path, "init", "-q")
    git_command(tmp_path, "config", "user.name", "Test User")
    git_command(tmp_path, "config", "user.email", "test@example.com")
    tracked = tmp_path / "main.py"
    tracked.write_text("print('before')\n", encoding="utf-8")
    git_command(tmp_path, "add", "main.py")
    git_command(tmp_path, "commit", "-qm", "baseline")
    tracked.write_text("print('after')\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("print('new')\n", encoding="utf-8")

    result = execute(tmp_path, "git_diff", {})

    assert result.success is True
    assert result.output["repository"] is True
    assert result.output["has_changes"] is True
    assert "diff --git a/main.py b/main.py" in result.output["diff"]
    assert "+print('after')" in result.output["diff"]
    assert any(line.startswith("?? new.py") for line in result.output["status"])
    assert result.output["status_truncated"] is False
    assert ".git" not in result.output["diff"]

    limited = execute(tmp_path, "git_diff", {"max_chars": 10})

    assert limited.success is True
    assert limited.output["diff_truncated"] is True
    assert limited.output["status_truncated"] is True


def test_git_diff_reports_non_repository_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    result = execute(tmp_path, "git_diff", {})

    assert result.success is False
    assert result.output["repository"] is False
    assert result.error
