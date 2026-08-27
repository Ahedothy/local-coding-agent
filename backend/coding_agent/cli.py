"""CLI composition layer for the local Agent Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from coding_agent.agent import Agent, Session
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent
from coding_agent.models import ModelResponse, MockModelProvider
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
    parser.add_argument("task", help="Programming task for the agent.")
    return parser


def print_event(event: AgentEvent) -> None:
    """Print one structured event without putting orchestration in the CLI."""
    payload = json.dumps(event.payload, ensure_ascii=False, default=str)
    print(f"[{event.type.value}] {payload}")


def _build_mock_agent(workspace: Workspace, event_handler) -> Agent:
    provider = MockModelProvider(
        [ModelResponse(content="Mock provider completed the task without tool calls.")]
    )
    registry = ToolRegistry((*FILESYSTEM_TOOLS, *EDIT_TOOLS, *COMMAND_TOOLS))
    tool_context = ToolContext(session_id="pending", workspace=workspace)
    session = Session(workspace_root=workspace.root)
    tool_context.session_id = session.session_id
    executor = ToolExecutor(registry, event_handler=event_handler)
    return Agent(
        provider,
        executor,
        ContextManager(SYSTEM_PROMPT),
        session,
        tool_context=tool_context,
        event_handler=event_handler,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, run Agent Core, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.provider == "real":
        print(
            "The real model provider is not implemented yet; use --provider mock.",
            file=sys.stderr,
        )
        return 2

    try:
        workspace = Workspace(args.workspace)
        agent = _build_mock_agent(workspace, print_event)
        result = asyncio.run(agent.run(args.task))
    except Exception as exc:
        print(f"Unable to start agent: {exc}", file=sys.stderr)
        return 2

    if result.final_answer is not None:
        print("\nFinal answer:")
        print(result.final_answer)
    else:
        print(f"\nAgent did not complete: {result.error}", file=sys.stderr)
    return 0 if result.status.value == "completed" else 1

if __name__ == "__main__":
    main()
