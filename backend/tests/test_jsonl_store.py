from __future__ import annotations

import json
from pathlib import Path

from coding_agent.events import AgentEvent, AgentEventType, JsonlEventLogger


def make_event(event_type: AgentEventType = AgentEventType.SESSION_STARTED) -> AgentEvent:
    return AgentEvent(
        type=event_type,
        session_id="session-1",
        payload={"message": "hello"},
    )


def test_jsonl_logger_writes_one_json_object_per_event(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "events.jsonl"
    logger = JsonlEventLogger(path)

    assert logger.write(make_event()) is True
    assert logger.write(make_event(AgentEventType.AGENT_FINISHED)) is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["type"] for line in lines] == [
        "session_started",
        "agent_finished",
    ]
    assert all(json.loads(line)["session_id"] == "session-1" for line in lines)


def test_jsonl_logger_can_be_used_as_event_handler(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = JsonlEventLogger(path)

    logger(make_event())

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["event_id"]


def test_jsonl_logger_failure_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "existing-directory"
    path.mkdir()
    logger = JsonlEventLogger(path)

    assert logger.write(make_event()) is False


def test_cli_event_log_is_optional_and_records_events(tmp_path: Path, capsys) -> None:
    from coding_agent.cli import main

    event_log = tmp_path / "event_logs" / "run.jsonl"
    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
            "--provider",
            "mock",
            "--event-log",
            str(event_log),
            "inspect the project",
        ]
    )

    assert exit_code == 0
    assert "[session_started]" in capsys.readouterr().out
    lines = event_log.read_text(encoding="utf-8").splitlines()
    assert lines
    assert json.loads(lines[0])["type"] == "session_started"
