from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from coding_agent.agent import Agent, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.workspace import Workspace


DEMO_SOURCE = Path(__file__).resolve().parents[2] / "demo_task_manager"


def test_demo_agent_inspects_multifile_project_patches_and_retests(tmp_path: Path) -> None:
    workspace_root = tmp_path / "buggy_task_manager"
    shutil.copytree(
        DEMO_SOURCE,
        workspace_root,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    python_executable = sys.executable
    patch = """diff --git a/task_manager/service.py b/task_manager/service.py
--- a/task_manager/service.py
+++ b/task_manager/service.py
@@ -33,11 +33,10 @@
         tasks = list(self.store.all())
         if query.strip():
-            needle = query.strip()
-            tasks = [task for task in tasks if needle in task.title]
+            needle = query.strip().casefold()
+            tasks = [task for task in tasks if needle in task.title.casefold()]
         if status is not None:
             tasks = [task for task in tasks if task.status == status]
__PATCH_BLANK__
-        return tasks[offset + 1 : offset + 1 + limit]
+        return tasks[offset : offset + limit]
__PATCH_BLANK__
     def set_status(self, task_id: int, status: str) -> Task:
         return self.store.update_status(task_id, status)
diff --git a/task_manager/report.py b/task_manager/report.py
--- a/task_manager/report.py
+++ b/task_manager/report.py
@@ -15,7 +15,6 @@
     for task in items:
         counts[task.status] += 1
     return {
-        "total": counts["done"],
+        "total": len(items),
         "by_status": counts,
         "titles": [task.title for task in items],
     }
""".replace("__PATCH_BLANK__", " ")
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(id="list-files", name="list_files", arguments={"recursive": True})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="read-tests", name="read_file", arguments={"path": "test_task_manager.py"})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="read-service", name="read_file", arguments={"path": "task_manager/service.py"})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="read-report", name="read_file", arguments={"path": "task_manager/report.py"})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="run-before",
                    name="execute_command",
                    arguments={"command": [python_executable, "-m", "pytest", "-q"]},
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="fix-multifile", name="apply_patch", arguments={"patch": patch})
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="run-after",
                    name="execute_command",
                    arguments={"command": [python_executable, "-m", "pytest", "-q"]},
                )
            ]
        ),
        ModelResponse(content="Fixed task search, pagination, and report aggregation; all tests pass."),
    ]
    provider = MockModelProvider(responses)
    events: list = []
    registry = ToolRegistry((*FILESYSTEM_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS))
    executor = ToolExecutor(registry, event_handler=events.append)
    session = Session(workspace_root=workspace_root)
    agent = Agent(
        provider,
        executor,
        ContextManager("You are a local coding agent."),
        session,
        tool_context=ToolContext(
            session_id=session.session_id,
            workspace=Workspace(workspace_root),
        ),
        event_handler=events.append,
    )

    result = asyncio.run(
        agent.run(
            "Fix the failing tests: read the code, run the tests, make the minimal "
            "change, and run the tests again."
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert result.final_answer is not None
    assert result.final_answer.startswith(
        "Fixed task search, pagination, and report aggregation; all tests pass."
    )
    assert "## Changes" in result.final_answer
    assert "diff --git a/task_manager/service.py b/task_manager/service.py" in result.final_answer
    assert "diff --git a/task_manager/report.py b/task_manager/report.py" in result.final_answer
    assert result.tool_calls == 7
    service_source = (workspace_root / "task_manager" / "service.py").read_text(encoding="utf-8")
    report_source = (workspace_root / "task_manager" / "report.py").read_text(encoding="utf-8")
    assert "needle = query.strip().casefold()" in service_source
    assert "return tasks[offset : offset + limit]" in service_source
    assert '"total": len(items)' in report_source
    assert [
        event.payload.get("tool_name")
        for event in events
        if event.type.value == "tool_finished"
    ] == [
        "list_files",
        "read_file",
        "read_file",
        "read_file",
        "apply_patch",
        "execute_command",
    ]
    assert any(
        event.type.value == "tool_failed"
        and event.payload.get("tool_name") == "execute_command"
        for event in events
    )
