"""FastAPI adapter that exposes Agent events as an SSE stream."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from coding_agent.agent import Agent, AgentRunResult, Session, SessionStatus
from coding_agent.agent.instructions import load_workspace_instructions
from coding_agent.config import load_dotenv
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, SqliteRunStore
from coding_agent.models import OpenAICompatibleProvider
from coding_agent.tools import ApprovalGate, ChangeStore, ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.tools.edit import EDIT_TOOLS
from coding_agent.tools.filesystem import FILESYSTEM_TOOLS
from coding_agent.tools.environment import ENVIRONMENT_TOOLS
from coding_agent.tools.inspection import INSPECTION_TOOLS
from coding_agent.tools.process import ManageProcessTool
from coding_agent.workspace import Workspace

SYSTEM_PROMPT = (
    "You are a local coding agent. Inspect and modify files only through the "
    "registered workspace tools. When the user communicates in Chinese, use "
    "Chinese for plans, explanations, reflections, and final answers unless "
    "the user asks for another language; keep code, commands, paths, and raw "
    "test output unchanged. "
    "Use execute_command with an argument array, "
    "not a shell string. On Windows, run workspace-local executables with "
    ".\\program.exe. Treat a command as successful only when the tool result "
    "has success=true, returncode=0, and the expected stdout is present. "
    "The Workspace boundary restricts direct file tools and validates command "
    "cwd, but a child process is not an operating-system sandbox and still runs "
    "with the current user's permissions; never use commands to access resources "
    "outside the selected workspace. "
    "Use search_files to locate code before reading many files; prefer regex "
    "search with a small context_lines value when looking for definitions, "
    "call sites, TODOs, or error messages across a project. "
    "When the task depends on installed runtimes, compilers, package managers, "
    "project markers, or local development ports, use inspect_environment first. "
    "It is read-only and only performs fixed local version probes; never use it "
    "as a substitute for arbitrary command execution. "
    "For long-running local services or interactive stdin tasks, use "
    "manage_process with start/list/status/read/write/stop. Keep the returned "
    "process_id, use an argument array, and stop processes when verification is "
    "finished; do not simulate a persistent service with repeated execute_command "
    "calls. "
    "Before reading or editing an unfamiliar file, use get_file_info when its "
    "type, binary status, or encoding is uncertain. Respect its file_type, "
    "mime_type, encoding, encoding_confidence, and detection method; do not "
    "send known binary files to read_file or text editing tools. "
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
    "plus added lines; even a blank context line must start with one space. "
    "When a patch is complex or you are unsure about line counts, first call "
    "apply_patch with dry_run=true. If the dry-run succeeds and the generated "
    "diff matches the requested edit, call apply_patch again with the same patch "
    "and dry_run=false. If "
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
    "When a command is intended to validate the task, set verification_kind to "
    "test, build, lint, typecheck, smoke, config, benchmark, scope, or other, "
    "and provide concise verification_criteria. Tests are only one kind of "
    "evidence: for projects without tests, use a relevant build, type check, "
    "configuration check, or behavior smoke test. Never claim verification from "
    "assistant text alone; rely on the local command result. "
    "read_file returns a sha256 content fingerprint. When editing a file that "
    "was previously read, pass that value as expected_sha256 (or use the "
    "expected_sha256 path map for apply_patch). If the fingerprint no longer "
    "matches, reread the file and regenerate the edit; never overwrite a stale "
    "file based on old context. "
    "For non-trivial tasks, "
    "state a concise 1-3 bullet plan in assistant text before the first tool "
    "call. After tool results, briefly state the next adjustment when useful; "
    "simple tasks may proceed directly. Never claim a tool result you did not "
    "receive. "
    "Only call tools whose names appear in the registered tool list; do not "
    "invent aliases such as edit_file, run_shell, or terminal. If a tool call "
    "fails with unknown tool, use the exact registered name shown in the error."
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


def _title_from_first_message(message: str) -> str:
    """Create a stable local history title from the first message."""
    title = " ".join(message.split()).strip()
    return title[:36].rstrip() + ("..." if len(title) > 36 else "")


def _default_history_database() -> Path:
    configured = os.getenv("CODING_AGENT_HISTORY_DIR")
    if configured:
        configured_path = Path(configured).expanduser()
        return configured_path if configured_path.suffix.casefold() == ".db" else configured_path / "history.db"
    return Path(__file__).resolve().parents[2] / "history.db"


def _default_agent_factory(
    workspace: Workspace,
    event_handler: Callable[[AgentEvent], object],
) -> Agent:
    """Compose the existing Agent Core for an API-created session."""
    load_dotenv()
    registry = ToolRegistry(
        (*FILESYSTEM_TOOLS, *INSPECTION_TOOLS, *ENVIRONMENT_TOOLS, ManageProcessTool(), *EDIT_TOOLS, *COMMAND_TOOLS)
    )
    session = Session(workspace_root=workspace.root)
    workspace_instructions = load_workspace_instructions(workspace.root)
    tool_context = ToolContext(session_id=session.session_id, workspace=workspace)
    executor = ToolExecutor(registry, event_handler=event_handler)
    return Agent(
        OpenAICompatibleProvider(),
        executor,
        ContextManager(SYSTEM_PROMPT + workspace_instructions.prompt_fragment()),
        session,
        tool_context=tool_context,
        event_handler=event_handler,
        workspace_instructions=workspace_instructions,
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
    history_store: SqliteRunStore | None = None

    async def append_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        if self.history_store is not None:
            self.history_store.append(self.run_id, event)
            self.history_store.save_session(
                self.agent.session,
                self.agent.context_manager,
            )
            if event.type.value == "user_message":
                content = event.payload.get("content")
                if isinstance(content, str):
                    self.history_store.set_session_title_if_default(
                        event.session_id,
                        _title_from_first_message(content),
                    )
            self.history_store.save_run_state(
                self.run_id,
                self.agent.session,
                self.agent.context_manager,
                self.agent.runtime_state(),
            )
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
    """Live API state backed by local SQLite run history."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        history_store: SqliteRunStore | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        self.history_store = history_store or SqliteRunStore(
            _default_history_database(),
            initialize=False,
        )
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}

    async def _on_event(self, session: SessionRecord, event: AgentEvent) -> None:
        if session.current_run is None:
            return
        await session.current_run.append_event(event)

    def _create_session_record(
        self,
        workspace: Workspace,
        *,
        restored_session: Session | None = None,
        restored_context: ContextManager | None = None,
    ) -> SessionRecord:
        holder: dict[str, SessionRecord | None] = {"record": None}

        async def on_event(event: AgentEvent) -> None:
            record = holder["record"]
            if record is not None:
                await self._on_event(record, event)

        agent = self.agent_factory(workspace, on_event)
        if restored_session is not None:
            restored_session.status = SessionStatus.IDLE
            agent.session = restored_session
            agent.tool_context.session_id = restored_session.session_id
            agent.tool_context.workspace = workspace
            # Emit a fresh session_started event for the resumed run while
            # keeping the restored conversation context intact.
            agent._session_started = False
        if restored_context is not None:
            agent.context_manager = restored_context
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

    def create_session(self, workspace: Workspace) -> SessionRecord:
        return self._create_session_record(workspace)

    def resume_history(self, run_id: str) -> SessionRecord:
        """Restore a completed session snapshot so a later turn can continue."""
        events = self.history_store.read(run_id)
        if not events:
            raise ValueError("run history is empty")
        session_id = events[0].session_id
        saved = self.history_store.load_run_state(run_id)
        if saved is None:
            raise ValueError("this run has no resumable session snapshot")
        existing = self.sessions.get(session_id)
        if existing is not None:
            if existing.current_run is not None and not existing.current_run.finished:
                raise ValueError("the historical session is already running")
        try:
            restored_session = Session.model_validate(saved["session"])
            workspace = Workspace(restored_session.workspace_root)
            restored_context = ContextManager.from_state(saved["context"])
        except Exception as exc:
            raise ValueError(f"could not restore historical session: {exc}") from exc
        record = self._create_session_record(
            workspace,
            restored_session=restored_session,
            restored_context=restored_context,
        )
        record.agent.restore_runtime_state(saved.get("runtime", {}))
        return record

    def activate_history(self, session_id: str) -> tuple[SessionRecord, RunRecord | None]:
        """Activate a session without cancelling any other live session run."""
        existing = self.sessions.get(session_id)
        if existing is not None:
            live_run = existing.current_run
            if live_run is not None and not live_run.finished:
                return existing, live_run
            return existing, None

        latest_run_id: str | None = None
        for run_id in self.history_store.list_run_ids():
            try:
                events = self.history_store.read(run_id)
            except ValueError:
                continue
            if events and events[0].session_id == session_id:
                latest_run_id = run_id
                break
        if latest_run_id is None:
            raise ValueError("session history is empty")

        saved = self.history_store.load_run_state(latest_run_id)
        if saved is None:
            raise ValueError("this session has no resumable state")
        try:
            restored_session = Session.model_validate(saved["session"])
            workspace = Workspace(restored_session.workspace_root)
            restored_context = ContextManager.from_state(saved["context"])
        except Exception as exc:
            raise ValueError(f"could not activate historical session: {exc}") from exc
        record = self._create_session_record(
            workspace,
            restored_session=restored_session,
            restored_context=restored_context,
        )
        record.agent.restore_runtime_state(saved.get("runtime", {}))
        return record, None

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
                history_store=self.history_store,
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


def create_app(
    agent_factory: AgentFactory | None = None,
    history_store: SqliteRunStore | None = None,
) -> FastAPI:
    """Create the API application with injectable Agent composition for tests."""
    state = ApiState(
        agent_factory or _default_agent_factory,
        history_store=history_store,
    )
    app = FastAPI(title="Local Coding Agent API")
    app.state.agent_state = state

    from .routes import register_routes

    register_routes(app, state)

    return app


app = create_app()

__all__ = ["app", "create_app"]
