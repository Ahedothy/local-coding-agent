from __future__ import annotations

from coding_agent.context import ContextManager
from coding_agent.models import ModelMessage, ToolCall
from coding_agent.tools import ToolResult


def test_system_user_and_assistant_messages_are_kept_in_order() -> None:
    context = ContextManager("You are a coding agent.")

    assert context.add_user_message("Inspect the project") is False
    assert context.add_assistant_message("I will inspect it.") is False

    messages = context.get_messages()
    assert [message.role for message in messages] == ["system", "user", "assistant"]
    assert messages[0].content == "You are a coding agent."
    assert messages[1].content == "Inspect the project"


def test_tool_result_becomes_a_model_tool_message() -> None:
    context = ContextManager("System")
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "main.py"})
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output={"path": "main.py", "content": "print('ok')"},
    )

    context.add_assistant_message(tool_calls=[call])
    assert context.add_tool_result(result) is False
    message = context.get_messages()[-1]
    assert message == ModelMessage(
        role="tool",
        content='{"success": true, "output": {"path": "main.py", "content": "print(\'ok\')"}, "error": null}',
        tool_call_id="call-1",
        name="read_file",
    )


def test_long_tool_result_is_truncated_before_context_insertion() -> None:
    context = ContextManager("System", max_tool_result_chars=40)
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "main.py"})
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output={"content": "x" * 200},
    )

    context.add_assistant_message(tool_calls=[call])
    assert context.add_tool_result(result) is True
    content = context.get_messages()[-1].content
    assert content is not None
    assert len(content) == 40
    assert content.endswith("... [tool result truncated]")


def test_budget_removes_old_messages_but_preserves_system_and_latest_user() -> None:
    context = ContextManager("system", max_chars=45)
    context.add_user_message("old user task")
    context.add_assistant_message("old assistant response")
    context.add_user_message("latest task must remain")
    context.add_assistant_message("new response")

    messages = context.get_messages()
    assert messages[0].role == "system"
    assert messages[0].content == "system"
    assert any(message.content == "latest task must remain" for message in messages)
    assert not any(message.content == "old assistant response" for message in messages)
    assert context.last_truncated is True


def test_assistant_tool_calls_are_preserved_as_model_messages() -> None:
    context = ContextManager("System")
    call = ToolCall(id="call-1", name="list_files", arguments={"path": "."})

    context.add_assistant_message(tool_calls=[call])

    message = context.get_messages()[-1]
    assert message.role == "assistant"
    assert message.tool_calls == [call]


def test_tool_call_and_tool_result_are_kept_as_one_context_group() -> None:
    context = ContextManager("System", max_chars=500)
    call = ToolCall(id="call-1", name="list_files", arguments={"path": "."})
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="list_files",
        success=True,
        output={"files": ["main.py"]},
    )

    context.add_user_message("Inspect the project")
    context.add_assistant_message(tool_calls=[call])
    context.add_tool_result(result)

    messages = context.get_messages()
    assert [message.role for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[-1].tool_call_id == call.id


def test_tool_call_and_tool_result_are_removed_together_when_budget_is_too_small() -> None:
    # The result fits by itself, but the assistant tool-call plus result does
    # not. The old per-message truncation could therefore leave an orphan tool.
    context = ContextManager("System", max_chars=74)
    call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "a-very-long-file-name.py"},
    )
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output={"content": "result"},
    )

    context.add_user_message("Task")
    context.add_assistant_message(tool_calls=[call])
    context.add_tool_result(result)

    messages = context.get_messages()
    assert not any(message.role == "tool" for message in messages)
    assert not any(message.role == "assistant" and message.tool_calls for message in messages)


def test_context_compacts_old_turns_into_structured_memory() -> None:
    context = ContextManager("System", max_chars=220, recent_message_groups=1)
    context.add_user_message("Fix the parser and keep the public API unchanged")
    context.add_assistant_message("I inspected the parser implementation.")
    context.add_user_message("Please run the tests after the edit")
    context.add_assistant_message("I will edit parser.py and run pytest.")
    context.add_user_message("The latest task is still important")
    context.add_assistant_message("Working on the latest task")

    messages = context.get_messages()
    assert any(
        message.role == "system"
        and (message.content or "").startswith("[conversation memory]")
        for message in messages
    )
    assert context.stats.compaction_count >= 1
    assert context.stats.summary_chars > 0
    assert context.stats.compaction_count >= 1
    assert context.last_change.strategy == "deterministic_extractive_summary"
    assert any(message.content == "The latest task is still important" for message in messages)


def test_context_compacts_old_verbose_tool_results_before_dropping_groups() -> None:
    context = ContextManager("System", max_chars=5_000, max_tool_result_chars=4_000, recent_message_groups=1)
    old_call = ToolCall(id="old", name="execute_command", arguments={"command": ["pytest"]})
    context.add_user_message("Run the old check")
    context.add_assistant_message(tool_calls=[old_call])
    context.add_tool_result(
        ToolResult(
            tool_call_id="old",
            tool_name="execute_command",
            success=False,
            output={"command": "pytest", "returncode": 1, "stdout": "failure details " * 200},
            error="tests failed",
        )
    )
    middle_call = ToolCall(id="middle", name="execute_command", arguments={"command": ["build"]})
    context.add_user_message("Run the build")
    context.add_assistant_message(tool_calls=[middle_call])
    context.add_tool_result(
        ToolResult(tool_call_id="middle", tool_name="execute_command", success=True, output={"command": "build", "stdout": "build output " * 200})
    )
    latest_call = ToolCall(id="latest", name="read_file", arguments={"path": "main.py"})
    context.add_user_message("Inspect the latest file")
    context.add_assistant_message(tool_calls=[latest_call])
    context.add_tool_result(
        ToolResult(tool_call_id="latest", tool_name="read_file", success=True, output={"path": "main.py"})
    )

    old_tool = next(message for message in context.get_messages() if message.tool_call_id == "old")
    assert old_tool.content is not None
    assert '"context_compacted": true' in old_tool.content
    assert "failure details" in old_tool.content
    assert context.stats.compaction_count >= 1


def test_context_stats_report_message_and_tool_sizes() -> None:
    context = ContextManager("System")
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
    context.add_user_message("Inspect a.py")
    context.add_assistant_message(tool_calls=[call])
    context.add_tool_result(
        ToolResult(tool_call_id="call-1", tool_name="read_file", success=True, output={"path": "a.py"})
    )

    stats = context.stats.as_dict()
    assert stats["message_count"] == 4
    assert stats["user_message_count"] == 1
    assert stats["assistant_message_count"] == 1
    assert stats["tool_message_count"] == 1
    assert stats["tool_result_chars"] > 0
    assert stats["total_chars"] <= stats["max_chars"]


def test_context_state_round_trip_preserves_tool_message_contract() -> None:
    context = ContextManager("System", max_chars=500, recent_message_groups=2)
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "main.py"})
    context.add_user_message("Inspect main.py")
    context.add_assistant_message("I will inspect it.", tool_calls=[call])
    context.add_tool_result(
        ToolResult(
            tool_call_id="call-1",
            tool_name="read_file",
            success=True,
            output={"path": "main.py", "content": "print('ok')"},
        )
    )

    restored = ContextManager.from_state(context.to_state())

    assert restored.get_messages() == context.get_messages()
    assert restored.stats.as_dict() == context.stats.as_dict()


def test_compaction_never_splits_tool_call_group() -> None:
    context = ContextManager("System", max_chars=330, recent_message_groups=1)
    first_call = ToolCall(id="call-1", name="read_file", arguments={"path": "old.py"})
    context.add_user_message("Inspect old.py")
    context.add_assistant_message(tool_calls=[first_call])
    context.add_tool_result(
        ToolResult(tool_call_id="call-1", tool_name="read_file", success=True, output={"path": "old.py", "content": "x"})
    )
    latest_call = ToolCall(id="call-2", name="read_file", arguments={"path": "latest.py"})
    context.add_user_message("Inspect latest.py")
    context.add_assistant_message(tool_calls=[latest_call])
    context.add_tool_result(
        ToolResult(tool_call_id="call-2", tool_name="read_file", success=True, output={"path": "latest.py", "content": "y"})
    )

    messages = context.get_messages()
    tool_ids = {message.tool_call_id for message in messages if message.role == "tool"}
    assistant_ids = {call.id for message in messages if message.role == "assistant" for call in message.tool_calls}
    assert tool_ids <= assistant_ids
