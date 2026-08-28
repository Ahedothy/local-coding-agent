from __future__ import annotations

import asyncio
import importlib
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


def execute_patch(tmp_path: Path, arguments: dict) -> object:
    executor = ToolExecutor(ToolRegistry(EDIT_TOOLS))
    return asyncio.run(
        executor.execute(
            ToolCall(id="patch-1", name="apply_patch", arguments=arguments),
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


def test_apply_patch_updates_multiple_files_and_returns_standard_diff(tmp_path: Path) -> None:
    first = tmp_path / "calculator.py"
    second = tmp_path / "config.py"
    first.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    second.write_text("DEBUG = False\n", encoding="utf-8")
    patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1 +1 @@
-DEBUG = False
+DEBUG = True
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is True
    assert first.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert second.read_text(encoding="utf-8") == "DEBUG = True\n"
    assert result.output["diff_summary"] == {
        "files": 2,
        "hunks": 2,
        "lines_added": 2,
        "lines_removed": 2,
        "bytes_before": 49,
        "bytes_after": 48,
    }
    assert "diff --git a/calculator.py b/calculator.py" in result.output["diff"]
    assert "-    return a - b\n+    return a + b" in result.output["diff"]
    assert "--- a/config.py\n+++ b/config.py" in result.output["diff"]
    assert result.output["rolled_back"] is False


def test_apply_patch_applies_multiple_standard_hunks_to_one_file(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\nstatus = 'old'\n", encoding="utf-8")
    patch = """--- a/main.py
+++ b/main.py
@@ -1,1 +1,1 @@
-value = 1
+value = 2
@@ -2,1 +2,1 @@
-status = 'old'
+status = 'new'
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is True
    assert path.read_text(encoding="utf-8") == "value = 2\nstatus = 'new'\n"
    assert result.output["diff_summary"]["hunks"] == 2


def test_apply_patch_rejects_stale_hunk_without_writing_any_file(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("status = 'old'\n", encoding="utf-8")
    patch = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-missing
+changed
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is False
    assert "context mismatch" in result.error
    assert first.read_text(encoding="utf-8") == "value = 1\n"
    assert second.read_text(encoding="utf-8") == "status = 'old'\n"
    assert result.output["touched_files"] == []
    assert result.output["rolled_back"] is False


def test_apply_patch_rejects_binary_files(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"before\x00after")
    patch = """--- a/data.bin
+++ b/data.bin
@@ -1 +1 @@
-before
+changed
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is False
    assert "binary file" in result.error
    assert result.output["rejected_hunks"][0]["path"] == "data.bin"


def test_apply_patch_rejects_workspace_escape(tmp_path: Path) -> None:
    patch = """--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is False
    assert "outside the workspace" in result.error


def test_apply_patch_rolls_back_when_writing_a_later_file_fails(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    edit_module = importlib.import_module("coding_agent.tools.edit")
    original_writer = edit_module._atomic_write_bytes

    def flaky_writer(path: Path, content: bytes) -> None:
        if path.name == "second.py":
            raise OSError("simulated write failure")
        original_writer(path, content)

    monkeypatch.setattr(edit_module, "_atomic_write_bytes", flaky_writer)
    patch = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-one
+changed
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-two
+updated
"""

    result = execute_patch(tmp_path, {"patch": patch})

    assert result.success is False
    assert result.output["rolled_back"] is True
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"
