from __future__ import annotations

import json
from pathlib import Path

from coding_agent.events import AgentEvent, AgentEventType
from coding_agent.trace import (
    format_summary,
    format_timeline,
    load_events,
    main,
    summarize_events,
)


FIXTURE = Path(__file__).parent / "trace_sample.jsonl"


def test_trace_summary_reads_fixture_jsonl() -> None:
    events = load_events(FIXTURE)
    summary = summarize_events(events)

    assert summary.session_id == "session-trace"
    assert summary.workspace_root == "C:/workspace/demo"
    assert summary.tasks == ["Fix the failing tests"]
    assert summary.status == "completed"
    assert summary.iterations == 2
    assert summary.tool_calls == 1
    assert summary.model_requests == 2
    assert summary.model_responses == 2
    assert summary.model_usage == {
        "completion_tokens": 20,
        "prompt_tokens": 60,
        "total_tokens": 80,
    }
    assert summary.latest_context == {
        "total_chars": 240,
        "max_chars": 1000,
        "utilization": 0.24,
    }
    assert summary.commands[0]["returncode"] == 0
    assert summary.commands[0]["command"] == ["python", "-m", "pytest", "-q"]
    assert summary.final_answer == "Fixed the tests."
    assert summary.error is None
    assert summary.turn_durations_seconds == [0.26]


def test_trace_text_contains_debugging_summary_and_timeline() -> None:
    events = load_events(FIXTURE)
    text = format_summary(summarize_events(events))
    timeline = format_timeline(events)

    assert "Session: session-trace" in text
    assert "Iterations: 2" in text
    assert "returncode=0" in text
    assert "Final answer:" in text
    assert "model_request" in timeline
    assert "+000.190s tool_finished - execute_command (0.100s)" in timeline


def test_trace_timeline_keeps_plan_annotations_out_of_final_answer() -> None:
    plan = AgentEvent(
        type=AgentEventType.PLAN,
        session_id="session-1",
        iteration=1,
        payload={"content": "Plan: inspect then test", "tool_call_count": 1},
    )
    plan_message = AgentEvent(
        type=AgentEventType.ASSISTANT_MESSAGE,
        session_id="session-1",
        iteration=1,
        payload={"content": "Plan: inspect then test", "tool_call_count": 1},
    )
    final_message = AgentEvent(
        type=AgentEventType.ASSISTANT_MESSAGE,
        session_id="session-1",
        iteration=2,
        payload={"content": "Finished.", "tool_call_count": 0},
    )

    summary = summarize_events([plan_message, plan, final_message])

    assert summary.final_answer == "Finished."
    assert "plan - Plan: inspect then test" in format_timeline([plan_message, plan, final_message])


def test_trace_cli_supports_json_and_reports_invalid_logs(tmp_path: Path, capsys) -> None:
    assert main([str(FIXTURE), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["tools"][0]["duration_seconds"] == 0.1

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("not json\n", encoding="utf-8")
    assert main([str(invalid)]) == 2
    assert "invalid AgentEvent" in capsys.readouterr().err


def test_trace_empty_event_log_is_safe() -> None:
    summary = summarize_events([])

    assert summary.as_dict()["event_count"] == 0
    assert "(no events)" in format_timeline([])


def test_trace_loads_event_enum_values_from_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    event = AgentEvent(
        type=AgentEventType.SESSION_STARTED,
        session_id="session-1",
        payload={},
    )
    path.write_text(event.model_dump_json() + "\n", encoding="utf-8")

    assert load_events(path)[0].type is AgentEventType.SESSION_STARTED
