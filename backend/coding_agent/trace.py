"""Read-only summaries and timeline replay for JSONL Agent event logs."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from coding_agent.events import AgentEvent, AgentEventType


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _format_seconds(value: float | None) -> str:
    return "--" if value is None else f"{value:.3f}s"


def _timestamp(event: AgentEvent) -> float:
    return event.timestamp.timestamp()


@dataclass(slots=True)
class ToolTrace:
    tool_name: str
    tool_call_id: str
    success: bool | None = None
    duration_seconds: float | None = None
    error: str | None = None
    output: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "success": self.success,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "output": self.output,
        }


@dataclass(slots=True)
class TraceSummary:
    session_id: str | None
    workspace_root: str | None
    tasks: list[str]
    status: str | None
    run_duration_seconds: float | None
    turn_durations_seconds: list[float]
    iterations: int
    tool_calls: int
    model_requests: int
    model_responses: int
    model_usage: dict[str, int | float]
    latest_context: dict[str, Any] | None
    tools: list[ToolTrace]
    commands: list[dict[str, Any]]
    final_answer: str | None
    error: str | None
    event_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "tasks": self.tasks,
            "status": self.status,
            "run_duration_seconds": self.run_duration_seconds,
            "turn_durations_seconds": self.turn_durations_seconds,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "model_requests": self.model_requests,
            "model_responses": self.model_responses,
            "model_usage": self.model_usage,
            "latest_context": self.latest_context,
            "tools": [tool.as_dict() for tool in self.tools],
            "commands": self.commands,
            "final_answer": self.final_answer,
            "error": self.error,
            "event_count": self.event_count,
        }


def load_events(path: str | Path) -> list[AgentEvent]:
    """Load one AgentEvent per non-empty JSONL line with line diagnostics."""
    event_path = Path(path)
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read event log {event_path}: {exc}") from exc

    events: list[AgentEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(AgentEvent.model_validate_json(line))
        except Exception as exc:
            raise ValueError(
                f"invalid AgentEvent at {event_path}:{line_number}: {exc}"
            ) from exc
    return events


def _merge_usage(total: dict[str, int | float], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        numeric = _number(value)
        if numeric is not None:
            total[key] = total.get(key, 0) + numeric


def _output_dict(event: AgentEvent) -> dict[str, Any]:
    output = event.payload.get("output")
    return dict(output) if isinstance(output, dict) else {}


def summarize_events(events: Sequence[AgentEvent]) -> TraceSummary:
    """Build a deterministic debugging summary from recorded events."""
    if not events:
        return TraceSummary(
            session_id=None,
            workspace_root=None,
            tasks=[],
            status=None,
            run_duration_seconds=0.0,
            turn_durations_seconds=[],
            iterations=0,
            tool_calls=0,
            model_requests=0,
            model_responses=0,
            model_usage={},
            latest_context=None,
            tools=[],
            commands=[],
            final_answer=None,
            error=None,
            event_count=0,
        )

    session_id = events[0].session_id
    workspace_root: str | None = None
    tasks: list[str] = []
    status: str | None = None
    final_answer: str | None = None
    error: str | None = None
    iterations = 0
    model_requests = 0
    model_responses = 0
    model_usage: dict[str, int | float] = {}
    latest_context: dict[str, Any] | None = None
    turn_durations: list[float] = []
    tools_by_id: dict[str, ToolTrace] = {}
    tool_order: list[str] = []
    commands: list[dict[str, Any]] = []

    for event in events:
        payload = event.payload
        if event.type is AgentEventType.SESSION_STARTED:
            root = payload.get("workspace_root")
            if isinstance(root, str):
                workspace_root = root
        elif event.type is AgentEventType.USER_MESSAGE:
            content = payload.get("content")
            if isinstance(content, str):
                tasks.append(content)
        elif event.type is AgentEventType.ITERATION_STARTED:
            if event.iteration is not None:
                iterations = max(iterations, event.iteration)
        elif event.type is AgentEventType.MODEL_REQUEST:
            model_requests += 1
            context = payload.get("context")
            if isinstance(context, dict):
                latest_context = context
        elif event.type is AgentEventType.MODEL_RESPONSE:
            model_responses += 1
            _merge_usage(model_usage, payload.get("usage"))
        elif event.type is AgentEventType.TOOL_STARTED:
            tool_name = str(payload.get("tool_name") or "unknown")
            tool_call_id = str(payload.get("tool_call_id") or f"event-{event.event_id}")
            if tool_call_id not in tools_by_id:
                tools_by_id[tool_call_id] = ToolTrace(tool_name, tool_call_id)
                tool_order.append(tool_call_id)
        elif event.type in {AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
            tool_name = str(payload.get("tool_name") or "unknown")
            tool_call_id = str(payload.get("tool_call_id") or f"event-{event.event_id}")
            tool = tools_by_id.get(tool_call_id)
            if tool is None:
                tool = ToolTrace(tool_name, tool_call_id)
                tools_by_id[tool_call_id] = tool
                tool_order.append(tool_call_id)
            tool.success = payload.get("success") if isinstance(payload.get("success"), bool) else event.type is AgentEventType.TOOL_FINISHED
            duration = _number(payload.get("duration_seconds"))
            tool.duration_seconds = float(duration) if duration is not None else tool.duration_seconds
            tool.error = payload.get("error") if isinstance(payload.get("error"), str) else tool.error
            tool.output = _output_dict(event)
            if tool.tool_name == "execute_command":
                command = {
                    "tool_call_id": tool.tool_call_id,
                    "success": tool.success,
                    "command": tool.output.get("command", []),
                    "returncode": tool.output.get("returncode"),
                    "duration_seconds": tool.duration_seconds,
                    "timed_out": tool.output.get("timed_out", False),
                    "error": tool.error,
                }
                commands.append(command)
        elif event.type is AgentEventType.CONTEXT_COMPACTED or event.type is AgentEventType.CONTEXT_TRUNCATED:
            context = payload.get("context")
            if isinstance(context, dict):
                latest_context = context
        elif event.type is AgentEventType.ASSISTANT_MESSAGE:
            content = payload.get("content")
            tool_call_count = payload.get("tool_call_count", 0)
            if (
                isinstance(content, str)
                and content.strip()
                and tool_call_count == 0
            ):
                final_answer = content
        elif event.type in {AgentEventType.PLAN, AgentEventType.REFLECTION}:
            # Planning text is already stored as an assistant message; the
            # event adds a searchable phase marker for trace review.
            continue
        elif event.type is AgentEventType.AGENT_ERROR:
            candidate = payload.get("error")
            if isinstance(candidate, str):
                error = candidate
        elif event.type is AgentEventType.AGENT_FINISHED:
            candidate_status = payload.get("status")
            if isinstance(candidate_status, str):
                status = candidate_status
            candidate_iterations = _number(payload.get("iterations"))
            if candidate_iterations is not None:
                iterations = max(iterations, int(candidate_iterations))
            candidate_duration = _number(payload.get("duration_seconds"))
            if candidate_duration is not None:
                turn_durations.append(float(candidate_duration))
            candidate_error = payload.get("error")
            if isinstance(candidate_error, str):
                error = candidate_error

    if len(events) > 1:
        run_duration = max(0.0, _timestamp(events[-1]) - _timestamp(events[0]))
    else:
        run_duration = 0.0

    return TraceSummary(
        session_id=session_id,
        workspace_root=workspace_root,
        tasks=tasks,
        status=status,
        run_duration_seconds=run_duration,
        turn_durations_seconds=turn_durations,
        iterations=iterations,
        tool_calls=len(tool_order),
        model_requests=model_requests,
        model_responses=model_responses,
        model_usage=model_usage,
        latest_context=latest_context,
        tools=[tools_by_id[tool_id] for tool_id in tool_order],
        commands=commands,
        final_answer=final_answer,
        error=error,
        event_count=len(events),
    )


def format_summary(summary: TraceSummary) -> str:
    """Render a compact human-readable trace report."""
    lines = [
        "Agent Trace Summary",
        "===================",
        f"Session: {summary.session_id or '--'}",
        f"Workspace: {summary.workspace_root or '--'}",
        f"Status: {summary.status or '--'}",
        f"Duration: {_format_seconds(summary.run_duration_seconds)}",
        f"Iterations: {summary.iterations}",
        f"Tool calls: {summary.tool_calls}",
        f"Model requests/responses: {summary.model_requests}/{summary.model_responses}",
        f"Events: {summary.event_count}",
    ]
    if summary.tasks:
        lines.append("Tasks:")
        lines.extend(f"  {index}. {task}" for index, task in enumerate(summary.tasks, start=1))
    if summary.model_usage:
        usage = ", ".join(f"{key}={value}" for key, value in sorted(summary.model_usage.items()))
        lines.append(f"Model usage: {usage}")
    if summary.latest_context:
        context = summary.latest_context
        lines.append(
            "Context: "
            f"{context.get('total_chars', '--')}/{context.get('max_chars', '--')} chars"
        )
    if summary.tools:
        lines.append("Tools:")
        for tool in summary.tools:
            state = "ok" if tool.success else "failed" if tool.success is False else "started"
            lines.append(
                f"  - {tool.tool_name} [{state}] "
                f"{_format_seconds(tool.duration_seconds)}"
            )
    if summary.commands:
        lines.append("Commands:")
        for command in summary.commands:
            rendered_command = " ".join(str(part) for part in command.get("command", []))
            lines.append(
                f"  - {rendered_command or '--'} -> "
                f"returncode={command.get('returncode', '--')} "
                f"({_format_seconds(command.get('duration_seconds'))})"
            )
    if summary.final_answer:
        lines.extend(["Final answer:", summary.final_answer])
    if summary.error:
        lines.append(f"Error: {summary.error}")
    return "\n".join(lines)


def format_timeline(events: Sequence[AgentEvent]) -> str:
    """Render event order and relative timing for a read-only replay."""
    if not events:
        return "Event Timeline\n==============\n(no events)"
    start = _timestamp(events[0])
    lines = ["Event Timeline", "=============="]
    for event in events:
        relative = _timestamp(event) - start
        detail = ""
        if event.type is AgentEventType.USER_MESSAGE:
            detail = str(event.payload.get("content") or "")
        elif event.type in {AgentEventType.TOOL_STARTED, AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
            detail = str(event.payload.get("tool_name") or "tool")
            if event.type is not AgentEventType.TOOL_STARTED:
                duration = _number(event.payload.get("duration_seconds"))
                if duration is not None:
                    detail += f" ({duration:.3f}s)"
        elif event.type is AgentEventType.AGENT_FINISHED:
            detail = str(event.payload.get("status") or "")
        elif event.type is AgentEventType.AGENT_ERROR:
            detail = str(event.payload.get("error") or "")
        elif event.type in {AgentEventType.PLAN, AgentEventType.REFLECTION}:
            detail = str(event.payload.get("content") or "")
        lines.append(
            f"+{relative:07.3f}s {event.type.value}"
            + (f" - {detail}" if detail else "")
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize and replay a local coding agent JSONL event log."
    )
    parser.add_argument("event_log", type=Path, help="Path to an Agent JSONL event log.")
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="Append the chronological event timeline after the summary.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the summary as JSON instead of formatted text.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        events = load_events(args.event_log)
        summary = summarize_events(events)
    except ValueError as exc:
        print(f"Unable to read trace: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = summary.as_dict()
        if args.timeline:
            payload["timeline"] = [
                {
                    "event_id": event.event_id,
                    "type": event.type.value,
                    "timestamp": event.timestamp.isoformat(),
                    "iteration": event.iteration,
                    "payload": event.payload,
                }
                for event in events
            ]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        output = format_summary(summary)
        if args.timeline:
            output += "\n\n" + format_timeline(events)
        print(output)
    return 0


if __name__ == "__main__":
    main()
