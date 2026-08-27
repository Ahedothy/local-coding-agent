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
from coding_agent.workspace import Workspace


SYSTEM_PROMPT = (
    "You are a local coding agent. Inspect and modify files only through the "
    "registered workspace tools."
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
    registry = ToolRegistry((*FILESYSTEM_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS))
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
