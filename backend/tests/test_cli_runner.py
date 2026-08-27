from __future__ import annotations

from pathlib import Path

from coding_agent.cli import _build_agent, main
from coding_agent.models import ModelResponse, MockModelProvider
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
