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
demo_task_manager/
```

Recommended structure:

```text
demo_task_manager/
  README.md
  pyproject.toml
  task_manager/
    __init__.py
    models.py
    store.py
    service.py
    report.py
  test_task_manager.py
```

## Bug Design

The demo is intentionally small, deterministic, and multi-file. It contains an
in-memory `TaskStore`, a `TaskService` with search and pagination, and a report
function that aggregates status counts.

The failing tests require the agent to discover and fix three related defects:

- title search should be case-insensitive;
- pagination should use a zero-based offset;
- report `total` should count every task, not only completed tasks.

The expected code changes touch both `task_manager/service.py` and
`task_manager/report.py`, which makes the diff and verification more
representative of a real maintenance task than a one-file edge-case fix.

## User Prompt

Use this prompt for the demo:

```text
Please fix the failing tests in this small Python project. Inspect the tests and
implementation first, make the minimal multi-file change, and run the complete
test suite again. Do not change the public API or weaken the tests.
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
model_response: tool_call read_file test_task_manager.py
tool_started: read_file
tool_finished: read_file

iteration_started
model_request
model_response: tool_call read_file task_manager/service.py
tool_started: read_file
tool_finished: read_file

iteration_started
model_request
model_response: tool_call read_file task_manager/report.py
tool_started: read_file
tool_finished: read_file

iteration_started
model_request
model_response: tool_call execute_command ["python", "-m", "pytest", "-q"]
tool_started: execute_command
tool_finished: execute_command exit_code=1

iteration_started
model_request
model_response: tool_call apply_patch (service.py and report.py)
tool_started: apply_patch
tool_finished: apply_patch

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

- `apply_patch`
- changed `task_manager/service.py` and `task_manager/report.py`

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

## Multi-Turn Manual Verification

Milestone 14C also verifies the user-level multi-turn capability. This is
separate from the internal Agent loop: one user turn may still contain several
model and tool iterations.

Start one interactive session from the `backend/` directory:

```powershell
python -m coding_agent.cli `
  --workspace "C:\path\to\workspace" `
  --provider real `
  --interactive
```

Use this short sequence:

```text
你好，请简单介绍一下你能做什么。
请读取这个项目的文件，告诉我它的功能。
现在运行测试并修复失败的测试，尽量只做最小修改。
/exit
```

Verification points:

- the process stays alive between user messages
- each non-blank message produces a new `user_message` and agent run
- the second and later model requests can use previous conversation context
- tool events remain visible in the terminal
- `/exit` or `/quit` exits normally
- no new Agent is created for each input

The conversation is in memory only. It is intentionally not restored after the
process exits, and this milestone does not add SQLite, JSONL replay, FastAPI
sessions, React state, or a WebSocket.

The CLI mock provider is intentionally a one-shot smoke-test provider, so use
the real provider for this manual multi-turn check. Multi-turn behavior without
network access is covered by `backend/tests/test_agent_multiturn.py` and the
CLI tests use a deterministic mock provider setup. Before recording, rehearse
the real-provider bug-fix demo at least three times. Keep API keys out of the
terminal recording.

## What Not to Demo

Do not spend time showing:

- installation
- dependency downloads
- UI styling
- SQLite or storage internals
- long architecture documents
- speculative future features

The video should show real local tool calls and a real test failure-to-success loop.
