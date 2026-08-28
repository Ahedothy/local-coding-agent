"""FastAPI adapter that exposes Agent events as an SSE stream."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from coding_agent.agent import Agent, AgentRunResult, Session
from coding_agent.config import load_dotenv
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent
from coding_agent.models import OpenAICompatibleProvider
from coding_agent.tools import ApprovalGate, ChangeStore, ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.tools.inspection import INSPECTION_TOOLS
from coding_agent.workspace import Workspace

SYSTEM_PROMPT = (
    "You are a local coding agent. Inspect and modify files only through the "
    "registered workspace tools. Use execute_command with an argument array, "
    "not a shell string. On Windows, run workspace-local executables with "
    ".\\program.exe. Treat a command as successful only when the tool result "
    "has success=true, returncode=0, and the expected stdout is present. "
    "Choose the editing tool deliberately: use replace_in_file only for one "
    "small contiguous exact replacement, usually one line or short snippet. "
    "For edits affecting two or more lines, multiple locations, code structure, "
    "or multiple files, prefer apply_patch. Provide a standard unified diff "
    "with ---/+++ file headers and @@ hunks, after reading the current files. "
    "Copy source lines exactly from the latest read_file content; do not copy "
    "UI line numbers, Markdown emphasis, or code-fence markers into the patch. "
    "Assemble the complete patch before invoking apply_patch; never send only "
    "file headers or a partial hunk. For separate changes in one file, include "
    "all complete hunks in the same call. "
    "Order hunks from top to bottom using line numbers from the original file, "
    "not line numbers after earlier hunks are applied. "
    "For every hunk, make the old/new line counts exactly match the following "
    "context, removed, and added lines. Count each hunk explicitly: old count "
    "equals context lines plus removed lines, and new count equals context lines "
    "plus added lines; even a blank context line must start with one space. If "
    "apply_patch fails, never resend the same patch; read the file again and "
    "regenerate it. Only use replace_in_file as a fallback when the requested "
    "change is truly one small contiguous exact replacement. Never call "
    "apply_patch with only ---/+++ file "
    "headers: every file entry must contain at least one complete @@ hunk with "
    "actual context, removed, or added lines. If there is no complete hunk, do "
    "not call apply_patch; reread the file and regenerate it. When patch context "
    "does not match, reread the file and regenerate the hunk. "
    "After a requested verification command succeeds, summarize the result and "
    "finish instead of repeating the same verification. "
    "For non-trivial tasks, "
    "state a concise 1-3 bullet plan in assistant text before the first tool "
    "call. After tool results, briefly state the next adjustment when useful; "
    "simple tasks may proceed directly. Never claim a tool result you did not "
    "receive."
)


def _configure_windows_subprocess_loop() -> None:
    """Use the Windows loop implementation that supports asyncio subprocesses."""
    if sys.platform != "win32":
        return

    policy_type = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_type is not None and not isinstance(asyncio.get_event_loop_policy(), policy_type):
        asyncio.set_event_loop_policy(policy_type())


_configure_windows_subprocess_loop()

AgentFactory = Callable[[Workspace, Callable[[AgentEvent], object]], Agent]


def _default_agent_factory(
    workspace: Workspace,
    event_handler: Callable[[AgentEvent], object],
) -> Agent:
    """Compose the existing Agent Core for an API-created session."""
    load_dotenv()
    registry = ToolRegistry(
        (*FILESYSTEM_TOOLS, *INSPECTION_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS)
    )
    session = Session(workspace_root=workspace.root)
    tool_context = ToolContext(session_id=session.session_id, workspace=workspace)
    executor = ToolExecutor(registry, event_handler=event_handler)
    return Agent(
        OpenAICompatibleProvider(),
        executor,
        ContextManager(SYSTEM_PROMPT),
        session,
        tool_context=tool_context,
        event_handler=event_handler,
    )


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    agent: Agent
    events: list[AgentEvent] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    result: AgentRunResult | None = None
    error: str | None = None

    async def append_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        self.wakeup.set()


@dataclass
class SessionRecord:
    session_id: str
    workspace: Workspace
    agent: Agent
    approval_gate: ApprovalGate
    change_store: ChangeStore
    current_run: RunRecord | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ApiState:
    """In-memory API state; persistence is intentionally outside this milestone."""

    def __init__(self, agent_factory: AgentFactory) -> None:
        self.agent_factory = agent_factory
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}

    async def _on_event(self, session: SessionRecord, event: AgentEvent) -> None:
        if session.current_run is None:
            return
        await session.current_run.append_event(event)

    def create_session(self, workspace: Workspace) -> SessionRecord:
        holder: dict[str, SessionRecord | None] = {"record": None}

        async def on_event(event: AgentEvent) -> None:
            record = holder["record"]
            if record is not None:
                await self._on_event(record, event)

        agent = self.agent_factory(workspace, on_event)
        approval_gate = ApprovalGate()
        change_store = ChangeStore(workspace)
        agent.tool_executor.approval_gate = approval_gate
        agent.tool_executor.change_store = change_store
        record = SessionRecord(
            session_id=agent.session.session_id,
            workspace=workspace,
            agent=agent,
            approval_gate=approval_gate,
            change_store=change_store,
        )
        holder["record"] = record
        self.sessions[record.session_id] = record
        return record

    async def start_run(
        self,
        session: SessionRecord,
        task: str,
        auto_approve: bool = False,
    ) -> RunRecord:
        async with session.lock:
            if session.current_run is not None and not session.current_run.finished:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="session already has a running task",
                )
            session.approval_gate.set_auto_approval(auto_approve)
            run = RunRecord(
                run_id=str(uuid4()),
                session_id=session.session_id,
                agent=session.agent,
            )
            session.current_run = run
            self.runs[run.run_id] = run
            asyncio.create_task(self._execute_run(session, run, task))
            return run

    async def _execute_run(
        self,
        session: SessionRecord,
        run: RunRecord,
        task: str,
    ) -> None:
        try:
            run.result = await run.agent.run_turn(task)
        except Exception as exc:
            run.error = str(exc)
        finally:
            run.finished = True
            run.wakeup.set()
            async with session.lock:
                if session.current_run is run:
                    session.current_run = None


def create_app(agent_factory: AgentFactory | None = None) -> FastAPI:
    """Create the API application with injectable Agent composition for tests."""
    state = ApiState(agent_factory or _default_agent_factory)
    app = FastAPI(title="lvyiyou Coding Agent API")
    app.state.agent_state = state

    from .routes import register_routes

    register_routes(app, state)

    return app


app = create_app()

__all__ = ["app", "create_app"]
