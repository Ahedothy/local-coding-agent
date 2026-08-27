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
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output={"path": "main.py", "content": "print('ok')"},
    )

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
    result = ToolResult(
        tool_call_id="call-1",
        tool_name="read_file",
        success=True,
        output={"content": "x" * 200},
    )

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
