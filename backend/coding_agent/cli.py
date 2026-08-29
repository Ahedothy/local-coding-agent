"""CLI composition layer for the local Agent Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from coding_agent.agent import Agent, Session
from coding_agent.config import load_dotenv
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, JsonlEventLogger
from coding_agent.models import (
    ModelResponse,
    MockModelProvider,
    OpenAICompatibleProvider,
)
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
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
    "For non-trivial tasks, "
    "state a concise 1-3 bullet plan in assistant text before the first tool "
    "call. After tool results, briefly state the next adjustment when useful; "
    "simple tasks may proceed directly. Never claim a tool result you did not "
    "receive."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local coding agent.")
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Workspace directory available to local tools.",
    )
    parser.add_argument(
        "--provider",
        choices=("mock", "real"),
        default="mock",
        help="Model provider mode; real is reserved for Milestone 12.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Keep the same Agent session for multiple user turns.",
    )
    parser.add_argument(
        "--event-log",
        type=Path,
        help="Optionally append Agent events to a JSONL file.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Programming task for one-shot mode or the optional first turn.",
    )
    return parser


def print_event(event: AgentEvent) -> None:
    """Print one structured event without putting orchestration in the CLI."""
    payload = json.dumps(event.payload, ensure_ascii=False, default=str)
    print(f"[{event.type.value}] {payload}")


def _build_event_handler(event_log: Path | None):
    logger = JsonlEventLogger(event_log) if event_log is not None else None

    def handle(event: AgentEvent) -> None:
        print_event(event)
        if logger is not None:
            logger(event)

    return handle


def _build_agent(workspace: Workspace, provider, event_handler) -> Agent:
    registry = ToolRegistry(
        (*FILESYSTEM_TOOLS, *INSPECTION_TOOLS, *ENVIRONMENT_TOOLS, ManageProcessTool(), *EDIT_TOOLS, *COMMAND_TOOLS)
    )
    session = Session(workspace_root=workspace.root)
    tool_context = ToolContext(session_id=session.session_id, workspace=workspace)
    executor = ToolExecutor(registry, event_handler=event_handler)
    return Agent(
        provider,
        executor,
        ContextManager(SYSTEM_PROMPT),
        session,
        tool_context=tool_context,
        event_handler=event_handler,
    )


def _build_mock_agent(workspace: Workspace, event_handler) -> Agent:
    provider = MockModelProvider(
        [ModelResponse(content="Mock provider completed the task without tool calls.")]
    )
    return _build_agent(workspace, provider, event_handler)


def _build_real_agent(workspace: Workspace, event_handler) -> Agent:
    load_dotenv()
    return _build_agent(
        workspace,
        OpenAICompatibleProvider(),
        event_handler,
    )


def _print_result(result) -> None:
    """Print one turn's result without adding orchestration to the CLI."""
    if result.final_answer is not None:
        print("\nFinal answer:")
        print(result.final_answer)
    else:
        print(f"\nAgent did not complete: {result.error}", file=sys.stderr)


async def _run_interactive(agent: Agent, initial_task: str | None) -> None:
    """Read user turns and reuse one Agent until the user exits."""
    if initial_task is not None:
        _print_result(await agent.run_turn(initial_task))

    while True:
        try:
            user_message = input("> ")
        except EOFError:
            print()
            return

        if user_message.strip() in {"/exit", "/quit"}:
            return
        if not user_message.strip():
            continue
        _print_result(await agent.run_turn(user_message))


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, run Agent Core, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.interactive and args.task is None:
        parser.error("the following arguments are required: task")

    try:
        workspace = Workspace(args.workspace)
        event_handler = _build_event_handler(args.event_log)
        if args.provider == "mock":
            agent = _build_mock_agent(workspace, event_handler)
        else:
            agent = _build_real_agent(workspace, event_handler)
        if args.interactive:
            asyncio.run(_run_interactive(agent, args.task))
            return 0
        result = asyncio.run(agent.run(args.task))
    except Exception as exc:
        print(f"Unable to start agent: {exc}", file=sys.stderr)
        return 2

    _print_result(result)
    return 0 if result.status.value == "completed" else 1

if __name__ == "__main__":
    main()
