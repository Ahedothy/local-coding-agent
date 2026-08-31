"""Deterministic offline task-level evaluation for the local coding agent."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from coding_agent.agent import Agent, AgentLimits, Session, SessionStatus
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ChangeStore, ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.workspace import Workspace

@dataclass(frozen=True, slots=True)
class EvalTask:
    name: str
    setup: object
    responses: tuple[ModelResponse, ...]
    allowed_paths: frozenset[str]
    grader: object

@dataclass(frozen=True, slots=True)
class EvalResult:
    name: str; completed: bool; verified: bool; tool_calls: int; model_calls: int; invalid_tool_calls: int; retries: int; duration_seconds: float; unsafe_writes: int; error: str | None = None
    def as_dict(self): return self.__dict__ if hasattr(self, "__dict__") else {field: getattr(self, field) for field in self.__dataclass_fields__}

@dataclass(slots=True)
class EvalReport:
    results: list[EvalResult]
    def as_dict(self):
        durations = sorted(item.duration_seconds for item in self.results)
        completed = sum(item.completed for item in self.results)
        return {"tasks": len(self.results), "completed": completed, "verified": sum(item.verified for item in self.results), "success_rate": completed / len(self.results) if self.results else 0.0, "median_tool_calls": sorted(item.tool_calls for item in self.results)[len(self.results)//2] if self.results else 0, "median_duration_seconds": durations[len(durations)//2] if durations else 0.0, "unsafe_writes": sum(item.unsafe_writes for item in self.results), "results": [item.as_dict() for item in self.results]}
    def format_text(self):
        s = self.as_dict(); return "\n".join([f"Tasks: {s['tasks']}", f"Completed: {s['completed']}", f"Verified: {s['verified']}", f"Success rate: {s['success_rate']:.1%}", f"Median tool calls: {s['median_tool_calls']}", f"Median duration: {s['median_duration_seconds']:.3f}s", f"Unsafe writes: {s['unsafe_writes']}"])

def _tasks():
    return (
        EvalTask("single_file_bug_fix", lambda root: (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8"), (ModelResponse(tool_calls=[_replace("c1", "calc.py", "return a - b", "return a + b")]), ModelResponse(content="Fixed add.")), frozenset({"calc.py"}), lambda root: "return a + b" in (root / "calc.py").read_text(encoding="utf-8")),
        EvalTask("tool_failure_recovery", lambda root: None, (ModelResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "missing.py"})]), ModelResponse(tool_calls=[ToolCall(id="c2", name="write_file", arguments={"path": "created.py", "content": "OK\n"})]), ModelResponse(content="Created the file.")), frozenset({"created.py"}), lambda root: (root / "created.py").read_text(encoding="utf-8") == "OK\n"),
        EvalTask("multi_file_change", lambda root: ((root / "config.py").write_text("ENABLED = False\n", encoding="utf-8"), (root / "app.py").write_text("from config import ENABLED\n", encoding="utf-8")), (ModelResponse(tool_calls=[_replace("c1", "config.py", "ENABLED = False", "ENABLED = True"), _replace("c2", "app.py", "from config import ENABLED", "from config import ENABLED  # feature flag")]), ModelResponse(content="Updated both files.")), frozenset({"config.py", "app.py"}), lambda root: "ENABLED = True" in (root / "config.py").read_text(encoding="utf-8") and "feature flag" in (root / "app.py").read_text(encoding="utf-8")),
    )

def _replace(call_id, path, old, new): return ToolCall(id=call_id, name="replace_in_file", arguments={"path": path, "old_text": old, "new_text": new})

async def _run(task):
    with tempfile.TemporaryDirectory(prefix="coding-agent-eval-") as directory:
        root = Path(directory); task.setup(root); events: list[AgentEvent] = []; provider = MockModelProvider(task.responses)
        workspace = Workspace(root); session = Session(workspace_root=root)
        executor = ToolExecutor(ToolRegistry((*FILESYSTEM_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS)), event_handler=events.append, change_store=ChangeStore(workspace))
        agent = Agent(provider, executor, ContextManager("You are a deterministic evaluation agent."), session, tool_context=ToolContext(session_id=session.session_id, workspace=workspace), event_handler=events.append, limits=AgentLimits(max_iterations=8))
        started = time.monotonic(); result = await agent.run_turn(f"Complete evaluation task: {task.name}")
        changed = set()
        for event in events:
            if event.type is AgentEventType.TOOL_FINISHED and isinstance(event.payload.get("output"), dict):
                output = event.payload["output"]; changed.update(str(path) for path in output.get("touched_files", []));
                if output.get("path"): changed.add(str(output["path"]))
        invalid = sum(event.type is AgentEventType.TOOL_FAILED and str(event.payload.get("error", "")).startswith("invalid arguments") for event in events)
        completed = result.status is SessionStatus.COMPLETED
        verified = bool(task.grader(root)) and completed
        return EvalResult(task.name, completed, verified, result.tool_calls, len(provider.requests), invalid, sum(event.type is AgentEventType.MODEL_RETRY_SCHEDULED for event in events), time.monotonic() - started, len(changed - set(task.allowed_paths)), result.error)

def run_mock_evaluation(): return EvalReport([asyncio.run(_run(task)) for task in _tasks()])

if __name__ == "__main__":
    report = run_mock_evaluation(); print(report.format_text()); print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
