from __future__ import annotations

from pathlib import Path

from coding_agent.cli import main


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


def test_cli_does_not_claim_real_provider_is_available(
    tmp_path: Path,
    capsys,
) -> None:
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
    assert "not implemented yet" in captured.err
