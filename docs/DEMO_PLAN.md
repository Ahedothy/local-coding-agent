# Demo Plan

## Demo Goal

The demo must prove that the project is a real coding agent, not just a chat UI.

The viewer should see:

- the agent receives a programming task
- the model requests local tools
- local tools read files
- local tools run tests
- local tools edit code
- the agent feeds tool results back into the model
- the agent runs tests again
- the final result passes

## Demo Repository

Location:

```text
demo/buggy_calculator/
```

Recommended structure:

```text
demo/buggy_calculator/
  README.md
  pyproject.toml
  calculator.py
  test_calculator.py
```

## Bug Design

The demo project should be very small and deterministic.

Recommended source:

```python
def add(a, b):
    return a + b

def divide(a, b):
    if b == 0:
        return 0
    return a / b

def average(numbers):
    return sum(numbers) / len(numbers)
```

Recommended tests:

```python
import pytest

from calculator import add, average, divide


def test_add():
    assert add(2, 3) == 5


def test_divide():
    assert divide(6, 3) == 2


def test_divide_by_zero():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_average():
    assert average([2, 4, 6]) == 4


def test_average_empty():
    with pytest.raises(ValueError):
        average([])
```

Expected bugs:

- `divide(1, 0)` returns `0` instead of raising `ValueError`
- `average([])` raises `ZeroDivisionError` instead of a clear `ValueError`

The correct fix is small:

- `divide` should raise `ValueError` when divisor is zero
- `average` should raise `ValueError` when the list is empty

## User Prompt

Use this prompt for the demo:

```text
Please fix the failing tests in this small Python project. Read the code, run the tests, make the minimal code change, and run the tests again.
```

## Ideal Agent Execution Trace

The exact model behavior can vary, but the ideal event sequence is:

```text
session_started
user_message

iteration_started
model_request
model_response: tool_call list_files
tool_started: list_files
tool_finished: list_files

iteration_started
model_request
model_response: tool_call read_file calculator.py
tool_started: read_file
tool_finished: read_file

iteration_started
model_request
model_response: tool_call read_file test_calculator.py
tool_started: read_file
tool_finished: read_file

iteration_started
model_request
model_response: tool_call execute_command ["python", "-m", "pytest", "-q"]
tool_started: execute_command
tool_finished: execute_command exit_code=1

iteration_started
model_request
model_response: tool_call replace_in_file
tool_started: replace_in_file
tool_finished: replace_in_file

iteration_started
model_request
model_response: tool_call execute_command ["python", "-m", "pytest", "-q"]
tool_started: execute_command
tool_finished: execute_command exit_code=0

iteration_started
model_request
model_response: final answer
assistant_message
agent_finished success
```

The video should not hide tool calls. The point is to show the loop.

## 2-Minute Video Timeline

### 0:00 - 0:10

Show the project title and one sentence:

```text
A self-implemented coding agent with local tools, workspace safety, and an explicit agent loop.
```

Mention that no Agent framework or Agent SDK is used.

### 0:10 - 0:25

Open the terminal.

Show:

- the demo workspace path
- the user task prompt
- the command used to start the CLI agent

Do not show API keys.

### 0:25 - 0:40

Show early event output:

- `session_started`
- `model_request`
- `model_response`
- `list_files`
- `read_file`

Narration point:

```text
The model does not read files directly. It asks for a tool call, and the local ToolExecutor runs it inside the workspace.
```

### 0:40 - 0:55

Show the first test run:

- `execute_command ["python", "-m", "pytest", "-q"]`
- failing pytest output

Narration point:

```text
The command is executed locally with a workspace cwd, timeout, and output limit.
```

### 0:55 - 1:15

Show code editing:

- `replace_in_file`
- changed `calculator.py`

If using an editor side by side, show the small diff or the updated function.

Narration point:

```text
The edit tool performs a controlled local replacement, not hosted code execution.
```

### 1:15 - 1:35

Show the second test run:

- `execute_command ["python", "-m", "pytest", "-q"]`
- all tests passing

This is the most important visual proof.

### 1:35 - 1:50

Show final assistant answer:

- summary of changes
- tests passed

Also show the event sequence enough to make the loop visible.

### 1:50 - 2:00

Show a final architecture snippet or README line:

```text
Agent Runtime -> ModelProvider -> ToolExecutor -> Workspace -> ContextManager -> next iteration
```

Close by emphasizing:

- self-implemented loop
- local tools
- workspace boundary
- deterministic tests with MockModelProvider

## Demo Stability Rules

Before recording:

- reset the demo repository to the failing state
- run the agent manually at least three times
- verify final pytest passes
- verify no API key appears in terminal history, logs, or video
- keep the prompt exactly the same across rehearsals
- keep the demo project small

## Fallback Demo

If the Web UI is not ready, use CLI only. CLI is enough for the evaluation because the scoring core is the Agent loop and local tool execution.

If the real model behaves unpredictably during recording, use a previously rehearsed run and keep the task small. Do not redesign the demo live.

## What Not to Demo

Do not spend time showing:

- installation
- dependency downloads
- UI styling
- SQLite or storage internals
- long architecture documents
- speculative future features

The video should show real local tool calls and a real test failure-to-success loop.
