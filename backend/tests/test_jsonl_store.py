from __future__ import annotations

import json
from pathlib import Path

from coding_agent.agent import Session
from coding_agent.context import ContextManager
from coding_agent.events import (
    AgentEvent,
    AgentEventType,
    JsonlEventLogger,
    JsonlRunStore,
    SqliteRunStore,
)


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


def test_jsonl_run_store_persists_and_reads_one_run(tmp_path: Path) -> None:
    store = JsonlRunStore(tmp_path / "history")
    run_id = "12345678-1234-1234-1234-123456789abc"

    assert store.append(run_id, make_event()) is True
    assert store.append(run_id, make_event(AgentEventType.AGENT_FINISHED)) is True

    assert store.list_run_ids() == [run_id]
    events = store.read(run_id)
    assert [event.type for event in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.AGENT_FINISHED,
    ]


def test_jsonl_run_store_rejects_path_like_run_ids(tmp_path: Path) -> None:
    store = JsonlRunStore(tmp_path / "history")

    assert store.append("..\\escape", make_event()) is False


def test_sqlite_run_store_persists_events_and_session_context(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "history.db")
    run_id = "12345678-1234-1234-1234-123456789abc"
    session = Session(workspace_root=tmp_path)
    context = ContextManager("System")
    context.add_user_message("Remember this task")

    assert store.append(
        run_id,
        AgentEvent(
            type=AgentEventType.SESSION_STARTED,
            session_id=session.session_id,
            payload={"workspace_root": str(tmp_path)},
        ),
    ) is True
    assert store.append(
        run_id,
        AgentEvent(
            type=AgentEventType.USER_MESSAGE,
            session_id=session.session_id,
            payload={"content": "Remember this task"},
        ),
    ) is True
    assert store.save_session(session, context) is True

    events = store.read(run_id)
    saved = store.load_session(session.session_id)
    assert len(events) == 2
    assert saved is not None
    assert saved["context"]["messages"][1]["content"] == "Remember this task"


def test_sqlite_run_store_keeps_a_snapshot_for_each_run(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "history.db")
    session = Session(workspace_root=tmp_path)
    context = ContextManager("System")
    first_run = "12345678-1234-1234-1234-123456789abc"
    second_run = "22345678-1234-1234-1234-123456789abc"

    for run_id, message in ((first_run, "first"), (second_run, "second")):
        store.append(
            run_id,
            AgentEvent(
                type=AgentEventType.SESSION_STARTED,
                session_id=session.session_id,
                payload={"workspace_root": str(tmp_path)},
            ),
        )
        context.add_user_message(message)
        store.append(
            run_id,
            AgentEvent(
                type=AgentEventType.USER_MESSAGE,
                session_id=session.session_id,
                payload={"content": message},
            ),
        )
        assert store.save_run_state(run_id, session, context, {"turn_index": 1}) is True

    first_state = store.load_run_state(first_run)
    second_state = store.load_run_state(second_run)
    assert first_state is not None
    assert second_state is not None
    assert [message["content"] for message in first_state["context"]["messages"] if message["role"] == "user"] == ["first"]
    assert [message["content"] for message in second_state["context"]["messages"] if message["role"] == "user"] == ["first", "second"]


def test_sqlite_session_title_is_set_once(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "history.db")
    session = Session(workspace_root=tmp_path)
    context = ContextManager("System")

    assert store.save_session(session, context) is True
    assert store.get_session_title(session.session_id) == "New conversation"
    assert store.set_session_title_if_default(session.session_id, "Fix the parser") is True
    assert store.get_session_title(session.session_id) == "Fix the parser"
    assert store.get_session_title_status(session.session_id) == "ready"
    assert store.set_session_title_if_default(session.session_id, "A later task") is False
    assert store.get_session_title(session.session_id) == "Fix the parser"
