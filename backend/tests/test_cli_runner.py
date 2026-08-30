from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.cli import (
    _Ansi,
    _build_cli_event_handler,
    _prompt_cli_approval,
    _print_answer,
    _resolve_cli_args,
    _build_agent,
    build_parser,
    main,
    print_event,
)
from coding_agent.events import AgentEvent, AgentEventType, SqliteRunStore
from coding_agent.models import ModelResponse, MockModelProvider, ToolCall
from coding_agent.tools import ApprovalGate
from coding_agent.workspace import Workspace


def test_cli_runs_mock_agent_and_prints_event_timeline(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "mock",
            "inspect the project",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[session_started]" in captured.out
    assert "[model_request]" in captured.out
    assert "Final answer:" in captured.out
    assert "Mock provider completed" in captured.out


def test_cli_rejects_invalid_workspace(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "--workspace",
            str(tmp_path / "missing"),
            "inspect the project",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Unable to start agent" in captured.err


def test_cli_reports_missing_real_provider_configuration(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")
    monkeypatch.chdir(tmp_path)
    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "real",
            "inspect the project",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "OPENAI_API_KEY is required" in captured.err


def test_cli_interactive_reuses_one_agent_for_multiple_turns(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    provider = MockModelProvider(
        [
            ModelResponse(content="first interactive answer"),
            ModelResponse(content="second interactive answer"),
        ]
    )
    workspace = Workspace(tmp_path)
    event_handler = lambda event: None
    agent = _build_agent(
        workspace,
        provider,
        event_handler,
    )
    monkeypatch.setattr("coding_agent.cli._build_mock_agent", lambda *_: agent)
    inputs = iter(["first turn", "second turn", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "mock",
            "--interactive",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "first interactive answer" in captured.out
    assert "second interactive answer" in captured.out
    assert len(provider.requests) == 2
    assert [message.content for message in provider.requests[1].messages][-3:] == [
        "first turn",
        "first interactive answer",
        "second turn",
    ]


def test_cli_interactive_ignores_blank_input_and_quit_exits_normally(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    provider = MockModelProvider([ModelResponse(content="answer")])
    agent = _build_agent(Workspace(tmp_path), provider, lambda event: None)
    monkeypatch.setattr("coding_agent.cli._build_mock_agent", lambda *_: agent)
    inputs = iter(["", "   ", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "mock",
            "--interactive",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert provider.requests == []
    assert "Final answer:" not in captured.out


def test_cli_short_form_defaults_to_real_and_infers_mode() -> None:
    parser = build_parser()

    args = parser.parse_intermixed_args(["project"])
    workspace, interactive, task = _resolve_cli_args(args, parser)
    assert workspace == Path("project")
    assert interactive is True
    assert task is None
    assert args.provider == "real"

    args = parser.parse_intermixed_args(["project", "fix tests"])
    workspace, interactive, task = _resolve_cli_args(args, parser)
    assert workspace == Path("project")
    assert interactive is False
    assert task == "fix tests"


def test_cli_named_workspace_form_remains_compatible() -> None:
    parser = build_parser()
    args = parser.parse_intermixed_args(["--workspace", "project", "fix tests"])
    workspace, interactive, task = _resolve_cli_args(args, parser)
    assert workspace == Path("project")
    assert interactive is False
    assert task == "fix tests"


def test_cli_default_event_view_hides_protocol_payloads(capsys) -> None:
    event = AgentEvent(
        type=AgentEventType.MODEL_RESPONSE,
        session_id="session",
        payload={"usage": {"prompt_tokens": 42}},
    )
    print_event(event, ansi=_Ansi(False))
    assert capsys.readouterr().out == ""

    print_event(
        AgentEvent(
            type=AgentEventType.PLAN,
            session_id="session",
            payload={"content": "先读取项目结构"},
        ),
        ansi=_Ansi(False),
    )
    assert "[plan] 计划: 先读取项目结构" in capsys.readouterr().out


def test_cli_raw_events_and_diff_rendering(capsys) -> None:
    print_event(
        AgentEvent(
            type=AgentEventType.SESSION_STARTED,
            session_id="session",
            payload={"workspace_root": "C:/project"},
        ),
        raw=True,
        ansi=_Ansi(False),
    )
    assert '"workspace_root"' in capsys.readouterr().out

    _print_answer("```diff\n+added\n-removed\n```", ansi=_Ansi(True))
    output = capsys.readouterr().out
    assert "\033[32m+added" in output
    assert "\033[31m-removed" in output


def test_cli_approval_patch_preview_preserves_diff_lines(capsys, monkeypatch) -> None:
    choices = iter(["d", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))
    _prompt_cli_approval(
        AgentEvent(
            type=AgentEventType.APPROVAL_REQUESTED,
            session_id="session",
            payload={
                "approval_id": "approval-1",
                "details": {
                    "patch": "diff --git a/app.py b/app.py\n"
                    "--- a/app.py\n"
                    "+++ b/app.py\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new\n"
                },
            },
        ),
        ApprovalGate(),
    )
    output = capsys.readouterr().out
    assert "  将修改文件：app.py" in output
    assert "  完整 diff：" in output
    assert "  已返回审批提示。" in output
    assert "    --- a/app.py" in output
    assert "    -old" in output
    assert "    +new" in output


def test_cli_command_approval_does_not_offer_diff(capsys, monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    _prompt_cli_approval(
        AgentEvent(
            type=AgentEventType.APPROVAL_REQUESTED,
            session_id="session",
            payload={
                "approval_id": "approval-command",
                "details": {
                    "command": ["pytest", "-q"],
                    "cwd": ".",
                },
            },
        ),
        ApprovalGate(),
    )
    output = capsys.readouterr().out
    assert "命令：pytest -q" in output
    assert "输入 y 允许本次" in output
    assert "查看完整 diff" not in output


def test_cli_final_answer_summarizes_changes_unless_requested(capsys) -> None:
    answer = (
        "已完成修改。\n\n## Changes\n\n```diff\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
        "-old\n+new\n```"
    )
    _print_answer(answer, ansi=_Ansi(False))
    summary = capsys.readouterr().out
    assert "已修改 1 个文件，新增 1 行，删除 1 行。" in summary
    assert "-old" not in summary
    assert "--show-diff" not in summary

    _print_answer(answer, ansi=_Ansi(False), show_diff=True)
    full = capsys.readouterr().out
    assert "-old" in full


def test_cli_approval_is_prompted_and_history_is_visible_to_web_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = MockModelProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={
                            "path": "created.txt",
                            "content": "created by cli",
                            "overwrite": True,
                            "create_parent_dirs": False,
                        },
                    )
                ]
            ),
            ModelResponse(content="写入完成"),
        ]
    )
    workspace = Workspace(tmp_path)
    gate = ApprovalGate()
    store = SqliteRunStore(tmp_path / "history.db")
    agent_ref = {"agent": None}
    handler = _build_cli_event_handler(
        None,
        raw_events=False,
        history_store=store,
        run_id="11111111-1111-1111-1111-111111111111",
        agent_ref=agent_ref,
        approval_gate=gate,
    )
    agent = _build_agent(workspace, provider, handler)
    agent_ref["agent"] = agent
    agent.tool_executor.approval_gate = gate
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    result = asyncio.run(agent.run("创建文件"))

    assert result.final_answer == "写入完成"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created by cli"
    events = store.read("11111111-1111-1111-1111-111111111111")
    event_types = [event.type for event in events]
    assert AgentEventType.APPROVAL_REQUESTED in event_types
    assert AgentEventType.APPROVAL_RESOLVED in event_types


def test_cli_ctrl_c_exits_without_traceback(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    agent = _build_agent(
        Workspace(tmp_path),
        MockModelProvider(),
        lambda _event: None,
    )
    monkeypatch.setattr("coding_agent.cli._build_mock_agent", lambda *_: agent)

    def interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "mock",
            "--interactive",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "已退出 CLI" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
