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
