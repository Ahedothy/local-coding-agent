"""CLI composition layer for the local Agent Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pydoc
import sys
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from coding_agent.agent import Agent, Session
from coding_agent.config import load_dotenv
from coding_agent.context import ContextManager
from coding_agent.events import AgentEvent, AgentEventType, JsonlEventLogger, SqliteRunStore
from coding_agent.models import (
    ModelResponse,
    MockModelProvider,
    OpenAICompatibleProvider,
)
from coding_agent.tools import ApprovalGate, ToolContext, ToolExecutor, ToolRegistry
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


def _shorten(text: str, limit: int = 320) -> str:
    """Keep activity lines readable when a model emits a long explanation."""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the local coding agent. With only a workspace path, starts an "
            "interactive session using the real provider."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        dest="workspace_option",
        help="Workspace directory available to local tools (legacy named form).",
    )
    parser.add_argument(
        "workspace_pos",
        nargs="?",
        type=Path,
        help="Workspace directory; positional form is recommended.",
    )
    parser.add_argument(
        "--provider",
        choices=("mock", "real"),
        default="real",
        help="Model provider mode (default: real).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--interactive",
        dest="interactive",
        action="store_true",
        help="Keep the same Agent session for multiple user turns.",
    )
    mode.add_argument(
        "--one-shot",
        dest="interactive",
        action="store_false",
        help="Run one task and exit; inferred when a task is provided.",
    )
    parser.set_defaults(interactive=None)
    parser.add_argument(
        "--event-log",
        type=Path,
        help="Optionally append Agent events to a JSONL file.",
    )
    parser.add_argument(
        "--raw-events",
        action="store_true",
        help="Print the full JSON event stream for diagnostics.",
    )
    parser.add_argument(
        "--show-diff",
        action="store_true",
        help="Include the complete accumulated diff in the final answer.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Programming task; optional when interactive mode is selected.",
    )
    return parser


def _event_text(event: AgentEvent) -> str:
    labels = {
        AgentEventType.SESSION_STARTED: "会话已创建",
        AgentEventType.USER_MESSAGE: "收到用户消息",
        AgentEventType.ITERATION_STARTED: f"第 {event.iteration or 0} 步",
        AgentEventType.MODEL_REQUEST: "正在分析任务",
        AgentEventType.MODEL_RESPONSE: "模型已返回结果",
        AgentEventType.TOOL_STARTED: "开始执行工具",
        AgentEventType.TOOL_FINISHED: "工具执行完成",
        AgentEventType.TOOL_FAILED: "工具执行失败",
        AgentEventType.APPROVAL_REQUESTED: "需要操作确认",
        AgentEventType.APPROVAL_RESOLVED: "操作确认完成",
        AgentEventType.ASSISTANT_MESSAGE: "Agent 回复",
        AgentEventType.PLAN: "计划",
        AgentEventType.REFLECTION: "正在检查结果",
        AgentEventType.CONTEXT_TRUNCATED: "已裁剪上下文",
        AgentEventType.CONTEXT_COMPACTED: "已压缩上下文",
        AgentEventType.AGENT_FINISHED: "Agent 已完成",
        AgentEventType.AGENT_ERROR: "运行已停止",
    }
    text = labels.get(event.type, event.type.value)
    if event.type in {AgentEventType.TOOL_STARTED, AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
        tool_name = event.payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            text = f"{text}: {tool_name}"
        output = event.payload.get("output")
        if isinstance(output, dict):
            command = output.get("command")
            path = output.get("path")
            if isinstance(command, list):
                text += f" — {_shorten(' '.join(str(part) for part in command), 200)}"
            elif isinstance(command, str):
                text += f" — {_shorten(command, 200)}"
            elif isinstance(path, str):
                text += f" — {_shorten(path, 200)}"
            if (
                event.type is AgentEventType.TOOL_FINISHED
                and isinstance(output.get("touched_files"), list)
            ):
                text += f"（更新 {len(output['touched_files'])} 个文件）"
    elif event.type in {AgentEventType.PLAN, AgentEventType.REFLECTION}:
        content = event.payload.get("content")
        if isinstance(content, str) and content.strip():
            text += f": {_shorten(content)}"
    elif event.type is AgentEventType.APPROVAL_REQUESTED:
        summary = event.payload.get("summary")
        if isinstance(summary, str) and summary.strip():
            text += f": {_shorten(summary)}"
    elif event.type is AgentEventType.AGENT_ERROR:
        error = event.payload.get("error")
        if isinstance(error, str) and error.strip():
            text += f": {_shorten(error)}"
    elif event.type is AgentEventType.AGENT_FINISHED:
        details: list[str] = []
        iterations = event.payload.get("iterations")
        tool_calls = event.payload.get("tool_calls")
        duration = event.payload.get("duration_seconds")
        if isinstance(iterations, int):
            details.append(f"{iterations} 步")
        if isinstance(tool_calls, int):
            details.append(f"工具调用 {tool_calls} 次")
        if isinstance(duration, (int, float)):
            details.append(f"用时 {duration:.1f} 秒")
        if details:
            text += "（" + "，".join(details) + "）"
    return text


class _Ansi:
    """Small dependency-free terminal styling helper."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = (
            enabled
            if enabled is not None
            else sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        )

    def wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self.wrap("2", text)

    def bold(self, text: str) -> str:
        return self.wrap("1", text)

    def green(self, text: str) -> str:
        return self.wrap("32", text)

    def red(self, text: str) -> str:
        return self.wrap("31", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def cyan(self, text: str) -> str:
        return self.wrap("36", text)


def print_event(
    event: AgentEvent,
    *,
    raw: bool = False,
    ansi: _Ansi | None = None,
) -> None:
    """Print a concise event summary, or the full event for diagnostics."""
    if raw:
        payload = json.dumps(event.payload, ensure_ascii=False, default=str)
        print(f"[{event.type.value}] {payload}")
        return
    # The structured event log still records every transition.  The terminal
    # view intentionally omits protocol-only messages that add little value
    # to a person watching a run (full assistant text is printed as the final
    # answer, while model response metadata remains available in --raw-events).
    if event.type in {
        AgentEventType.USER_MESSAGE,
        AgentEventType.MODEL_RESPONSE,
        AgentEventType.ASSISTANT_MESSAGE,
    }:
        return
    style = ansi or _Ansi(False)
    prefix = style.dim(f"[{event.type.value}]")
    text = _event_text(event)
    if event.type in {AgentEventType.TOOL_FAILED, AgentEventType.AGENT_ERROR}:
        text = style.red(text)
    elif event.type in {AgentEventType.TOOL_FINISHED, AgentEventType.AGENT_FINISHED}:
        text = style.green(text)
    elif event.type in {
        AgentEventType.PLAN,
        AgentEventType.REFLECTION,
        AgentEventType.APPROVAL_REQUESTED,
    }:
        text = style.cyan(text)
    else:
        text = style.dim(text)
    print(f"{prefix} {text}")


def _build_event_handler(event_log: Path | None, *, raw_events: bool = False):
    logger = JsonlEventLogger(event_log) if event_log is not None else None
    ansi = _Ansi()

    def handle(event: AgentEvent) -> None:
        print_event(event, raw=raw_events, ansi=ansi)
        if logger is not None:
            logger(event)

    return handle


def _default_history_database() -> Path:
    """Use the same SQLite history location as the Web API."""
    configured = os.environ.get("CODING_AGENT_HISTORY_DIR")
    if configured:
        configured_path = Path(configured).expanduser()
        return (
            configured_path
            if configured_path.suffix.casefold() == ".db"
            else configured_path / "history.db"
        )
    return Path(__file__).resolve().parents[1] / "history.db"


def _approval_file_list(event: AgentEvent) -> list[str]:
    details = event.payload.get("details")
    if not isinstance(details, dict):
        return []
    files: list[str] = []
    patch = details.get("patch")
    if isinstance(patch, str):
        for line in patch.splitlines():
            if line.startswith("+++ "):
                path = line[4:].split("\t", 1)[0].strip()
                if path.startswith("b/"):
                    path = path[2:]
                if path != "/dev/null":
                    files.append(path)
    command = details.get("command")
    path = details.get("path")
    if isinstance(path, str) and path:
        files.append(path)
    return list(dict.fromkeys(files))


def _approval_details_text(event: AgentEvent) -> str:
    details = event.payload.get("details")
    if not isinstance(details, dict):
        return ""
    command = details.get("command")
    if isinstance(command, list):
        return _shorten("命令：" + " ".join(str(part) for part in command), 240)
    files = _approval_file_list(event)
    if files:
        return _shorten("文件：" + "、".join(files), 240)
    return ""


def _render_diff_line(line: str, ansi: _Ansi) -> str:
    if line.startswith(("+++", "---")):
        return ansi.bold(line)
    if line.startswith("+"):
        return ansi.green(line)
    if line.startswith("-"):
        return ansi.red(line)
    if line.startswith("@@"):
        return ansi.cyan(line)
    return line


def _print_diff_line(line: str, ansi: _Ansi) -> None:
    print(_render_diff_line(line, ansi))


def _print_approval_patch(patch: str, *, force_pager: bool = False) -> None:
    """Print a bounded, line-preserving patch preview for CLI approval."""
    ansi = _Ansi()
    lines = patch.replace("\r\n", "\n").splitlines()
    max_lines = 32
    if force_pager and len(lines) > max_lines and sys.stdout.isatty():
        print("  补丁较长，进入分页查看（空格翻页，通常按 q 退出，退出后继续审批）：")
        rendered = "\n".join(
            f"    {_render_diff_line(line, ansi)}" for line in lines
        )
        pydoc.pager(rendered)
        return
    for line in lines[:max_lines]:
        print(f"    {_render_diff_line(line, ansi)}")
    if len(lines) > max_lines:
        print(f"    …（其余 {len(lines) - max_lines} 行未显示）")


def _prompt_cli_approval(event: AgentEvent, approval_gate: ApprovalGate) -> None:
    """Resolve a pending approval from the local terminal."""
    approval_id = event.payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return
    details = event.payload.get("details")
    patch = details.get("patch") if isinstance(details, dict) else None
    files = _approval_file_list(event)
    if files:
        print(f"  将修改文件：{_shorten('、'.join(files), 300)}")
    else:
        detail = _approval_details_text(event)
        if detail:
            print(f"  {detail}")
    can_view_diff = isinstance(patch, str) and bool(patch)
    if can_view_diff:
        print("  输入 d 查看完整 diff，y 允许本次，a 允许本轮，其他输入拒绝。")
    else:
        print("  输入 y 允许本次，a 允许本轮，其他输入拒绝。")
    while True:
        try:
            choice = input("  是否允许？ [y/N/d/a] ").strip().lower()
        except EOFError:
            choice = ""
        if not can_view_diff or choice not in {"d", "diff", "查看"}:
            break
        if isinstance(patch, str):
            print("  完整 diff：")
            _print_approval_patch(patch, force_pager=True)
        else:
            print("  当前操作没有可展开的 unified diff。")
        print("  已返回审批提示。")
    approved = choice in {"y", "yes", "是"}
    approve_current_turn = choice in {"a", "all", "always", "本轮"}
    approval_gate.decide(
        approval_id,
        approved or approve_current_turn,
        approve_current_turn=approve_current_turn,
    )


def _build_cli_event_handler(
    event_log: Path | None,
    *,
    raw_events: bool,
    history_store: SqliteRunStore,
    run_id: str,
    agent_ref: dict[str, Agent | None],
    approval_gate: ApprovalGate,
):
    """Combine concise terminal output, approvals, and Web-visible history."""
    logger = JsonlEventLogger(event_log) if event_log is not None else None
    ansi = _Ansi()

    def handle(event: AgentEvent) -> None:
        print_event(event, raw=raw_events, ansi=ansi)
        if logger is not None:
            logger(event)
        history_store.append(run_id, event)
        agent = agent_ref.get("agent")
        if agent is not None:
            history_store.save_session(agent.session, agent.context_manager)
            if event.type is AgentEventType.USER_MESSAGE:
                content = event.payload.get("content")
                if isinstance(content, str):
                    history_store.set_session_title_if_default(
                        event.session_id,
                        _shorten(content, 36),
                    )
            history_store.save_run_state(
                run_id,
                agent.session,
                agent.context_manager,
                {"turn_index": agent.turn_index},
            )
        if event.type is AgentEventType.APPROVAL_REQUESTED:
            _prompt_cli_approval(event, approval_gate)

    return handle


def _diff_summary(diff: str) -> str:
    files: list[str] = []
    added = 0
    removed = 0
    for line in diff.replace("\r\n", "\n").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    file_count = len(dict.fromkeys(files))
    summary = f"已修改 {file_count} 个文件，新增 {added} 行，删除 {removed} 行。"
    unique_files = list(dict.fromkeys(files))
    if unique_files:
        shown = unique_files[:8]
        file_text = "、".join(shown)
        if len(unique_files) > len(shown):
            file_text += f" 等 {len(unique_files)} 个文件"
        summary += f"\n文件：{file_text}"
    return summary


def _print_answer(
    answer: str,
    *,
    ansi: _Ansi | None = None,
    show_diff: bool = False,
) -> None:
    """Render a small, dependency-free subset of Markdown for terminal output."""
    style = ansi or _Ansi(False)
    if not show_diff:
        marker = "\n## Changes\n"
        marker_index = answer.find(marker)
        if marker_index >= 0:
            before = answer[:marker_index].rstrip()
            changes = answer[marker_index + len(marker) :]
            diff = ""
            if "```diff" in changes:
                diff = changes.split("```diff", 1)[1].split("```", 1)[0].strip()
            if before:
                _print_answer(before, ansi=style, show_diff=True)
                print()
            print(style.bold("Changes:"))
            if diff:
                print(_diff_summary(diff))
            else:
                print("已完成文件修改。")
            return
    in_diff = False
    for line in answer.replace("\r\n", "\n").split("\n"):
        if line.strip().lower() in {"```diff", "```patch"}:
            in_diff = True
            print(style.dim(line))
        elif in_diff and line.strip() == "```":
            in_diff = False
            print(style.dim(line))
        elif in_diff:
            _print_diff_line(line, style)
        elif line.startswith("#"):
            print(style.bold(line))
        elif line.startswith("```"):
            print(style.dim(line))
        else:
            print(line)


def _print_result(result, *, show_diff: bool = False) -> None:
    """Print one turn's result without adding orchestration to the CLI."""
    ansi = _Ansi()
    if result.final_answer is not None:
        print("\nFinal answer:")
        _print_answer(result.final_answer, ansi=ansi, show_diff=show_diff)
    else:
        print(f"\n{ansi.red('Agent did not complete')}: {result.error}", file=sys.stderr)


def _resolve_cli_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> tuple[Path, bool, str | None]:
    workspace = args.workspace_option or args.workspace_pos
    task = args.task
    if args.workspace_option is not None and args.workspace_pos is not None:
        if task is not None:
            parser.error("provide the workspace either positionally or with --workspace, not both")
        task = str(args.workspace_pos)
    if workspace is None:
        parser.error("a workspace directory is required")
    interactive = args.interactive if args.interactive is not None else task is None
    if not interactive and task is None:
        parser.error("a task is required in one-shot mode")
    return Path(workspace), interactive, task


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


async def _run_interactive(
    agent: Agent,
    initial_task: str | None,
    *,
    show_diff: bool = False,
) -> None:
    """Read user turns and reuse one Agent until the user exits."""
    print("Local Coding Agent")
    print("输入任务开始；输入 /help 查看提示，输入 /exit 或 /quit 退出。")
    if initial_task is not None:
        _print_result(await agent.run_turn(initial_task), show_diff=show_diff)

    while True:
        try:
            user_message = input("\n> ")
        except EOFError:
            print()
            return

        if user_message.strip() == "/help":
            print("直接输入编程任务开始执行；/exit 或 /quit 退出；空行会被忽略。")
            continue
        if user_message.strip() in {"/exit", "/quit"}:
            return
        if not user_message.strip():
            continue
        _print_result(await agent.run_turn(user_message), show_diff=show_diff)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, run Agent Core, and return a process exit code."""
    parser = build_parser()
    # parse_intermixed_args lets users place --one-shot/--provider between the
    # workspace path and task (for example: ``agent path --one-shot task``).
    # Fall back to parse_args for Python implementations without that API.
    if argv is None:
        argv = sys.argv[1:]
    parse = getattr(parser, "parse_intermixed_args", parser.parse_args)
    args = parse(argv)
    workspace_path, interactive, task = _resolve_cli_args(args, parser)

    try:
        workspace = Workspace(workspace_path)
        history_store = SqliteRunStore(_default_history_database())
        approval_gate = ApprovalGate()
        agent_ref: dict[str, Agent | None] = {"agent": None}
        event_handler = _build_cli_event_handler(
            args.event_log,
            raw_events=args.raw_events,
            history_store=history_store,
            run_id=str(uuid4()),
            agent_ref=agent_ref,
            approval_gate=approval_gate,
        )
        if args.provider == "mock":
            agent = _build_mock_agent(workspace, event_handler)
        else:
            agent = _build_real_agent(workspace, event_handler)
        agent_ref["agent"] = agent
        agent.tool_executor.approval_gate = approval_gate
        if interactive:
            asyncio.run(_run_interactive(agent, task, show_diff=args.show_diff))
            return 0
        result = asyncio.run(agent.run(task))
    except KeyboardInterrupt:
        # Ctrl+C is an expected way to stop a local run.  Do not expose an
        # asyncio traceback; 130 is the conventional shell exit status for
        # SIGINT, while the short message makes the outcome clear.
        print("\n已退出 CLI。")
        return 130
    except Exception as exc:
        print(f"Unable to start agent: {exc}", file=sys.stderr)
        return 2

    _print_result(result, show_diff=args.show_diff)
    return 0 if result.status.value == "completed" else 1

if __name__ == "__main__":
    main()
