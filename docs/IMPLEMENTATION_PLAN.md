# Implementation Plan

## Strategy

Build the project in this order:

```text
docs -> skeleton -> workspace -> core types -> tool system -> tools
     -> context -> mock model -> agent loop -> CLI -> real model
     -> demo -> multi-turn core -> interactive CLI -> event log
     -> README -> optional Web API -> optional React UI
```

Do not start with the Web UI. The first real goal is a CLI agent that can run a demo task end to end.

## Milestone 0: Documentation and Rules

Goal:

- lock the architecture and implementation constraints before coding

Files:

- `docs/ARCHITECTURE.md`
- `docs/IMPLEMENTATION_RULES.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/TEST_PLAN.md`
- `docs/DEMO_PLAN.md`
- `README.txt` later

Completion criteria:

- the docs clearly forbid Agent frameworks and Agent SDKs
- the docs clearly say that MVP is CLI-first
- the docs clearly remove SQLite, summary compaction, full apply_patch, and multi-provider support from MVP

Do not:

- write business logic
- connect a real model

## Milestone 1: Project Skeleton and Test Setup

Goal:

- create the Python package, test layout, and basic configuration

Expected structure:

```text
backend/
  coding_agent/
    __init__.py
    cli.py
    config.py
  tests/
pyproject.toml
.env.example
.gitignore
```

Completion criteria:

- `python -m pytest` runs
- the package imports cleanly
- no API key is committed

Do not:

- implement Agent Runtime
- implement concrete tools
- implement Web UI

## Milestone 2: Workspace Security

Goal:

- build the safety boundary all tools must use

Modules:

- `backend/coding_agent/workspace/workspace.py`
- `backend/coding_agent/workspace/security.py`
- `backend/tests/test_workspace_security.py`

Completion criteria:

- relative paths inside workspace are allowed
- `../` traversal is rejected
- absolute paths outside workspace are rejected
- symlink escapes are rejected
- `.git` access is rejected
- command cwd outside workspace is rejected

Tests:

- path resolution tests
- read/write eligibility tests
- cwd resolution tests
- symlink escape test where platform support allows it

Do not:

- implement tools before the path boundary is stable
- implement a full OS sandbox

## Milestone 3: Core Types

Goal:

- define stable data structures shared across modules

Modules:

- `backend/coding_agent/models/base.py`
- `backend/coding_agent/tools/base.py`
- `backend/coding_agent/events/event.py`
- `backend/coding_agent/agent/session.py`

Core types:

- `ModelMessage`
- `ModelRequest`
- `ModelResponse`
- `ToolCall`
- `ToolResult`
- `ToolContext`
- `AgentEvent`
- `Session`

Completion criteria:

- types are provider-neutral
- types do not import FastAPI or React concepts
- Pydantic models validate expected fields

Do not:

- implement OpenAI provider yet
- implement tool execution yet

## Milestone 4: ToolRegistry and ToolExecutor

Goal:

- implement the generic tool system before concrete tools

Modules:

- `backend/coding_agent/tools/registry.py`
- `backend/coding_agent/tools/executor.py`
- `backend/tests/test_tool_executor.py`

Completion criteria:

- tools can be registered
- duplicate names are rejected
- model-visible schemas can be exported
- unknown tool calls return a failed ToolResult
- invalid arguments return a failed ToolResult
- tool exceptions are caught and converted to ToolResult
- ToolExecutor uses ToolContext

Tests:

- mock tool success
- mock tool validation failure
- unknown tool
- duplicate registration
- tool exception handling

Do not:

- put tool routing logic inside Agent Runtime
- skip Pydantic validation

## Milestone 5: Filesystem Tools

Goal:

- allow the agent to inspect and edit workspace files safely

Modules:

- `backend/coding_agent/tools/filesystem.py`
- `backend/tests/test_tools_filesystem.py`

Tools:

- `list_files`
- `read_file`
- `write_file`
- `search_files`

Completion criteria:

- all paths go through Workspace
- large outputs are truncated
- binary file reads are rejected
- search ignores obvious dependency/cache directories
- errors are returned as ToolResult failures

Tests:

- normal list/read/write/search behavior
- path traversal rejection
- large file truncation
- binary file rejection
- search max match limit

Do not:

- implement complex `.gitignore` behavior
- read or write `.git`

## Milestone 6: `replace_in_file` Tool

Goal:

- provide a stable local code editing tool for the demo

Module:

- `backend/coding_agent/tools/edit.py`

Tool:

- `replace_in_file`

Parameters:

- `path`
- `old_text`
- `new_text`
- `expected_replacements`, default `1`

Completion criteria:

- unique old text is replaced
- missing old text returns failure
- multiple matches with expected `1` returns failure
- expected replacement count is enforced
- binary file edits are rejected
- workspace escape is rejected

Tests:

- successful replacement
- missing text
- multiple match protection
- expected count behavior
- path security

Do not:

- implement full unified diff `apply_patch` in MVP

## Milestone 7: `execute_command` Tool

Goal:

- allow the agent to run local tests and commands safely enough for a student MVP

Module:

- `backend/coding_agent/tools/command.py`

Rules:

- command is `list[str]`
- `shell=False`
- cwd must resolve inside workspace
- timeout is mandatory
- stdout and stderr are truncated
- obviously dangerous commands are rejected as defense in depth

Completion criteria:

- successful commands return exit code, stdout, stderr, duration
- failed commands return non-zero exit code and output with `success=false`
- timed-out commands are killed and reported
- cwd outside workspace is rejected

Tests:

- success
- failure
- timeout
- output truncation
- cwd escape
- blocked command

Do not:

- implement an interactive shell
- accept shell command strings by default

## Milestone 8: ContextManager

Goal:

- maintain model messages and enforce simple context limits

Modules:

- `backend/coding_agent/context/messages.py`
- `backend/coding_agent/context/manager.py`
- `backend/tests/test_context_manager.py`

Completion criteria:

- system prompt is preserved
- user messages can be appended
- assistant messages can be appended
- ToolResult is converted to a tool message
- long tool output is truncated before context insertion
- old messages are truncated when character budget is exceeded
- `context_truncated` event can be emitted by the caller

Do not:

- implement model-based summary compaction in MVP

## Milestone 9: MockModelProvider

Goal:

- make Agent loop tests deterministic

Module:

- `backend/coding_agent/models/mock.py`

Completion criteria:

- returns a queue of preconfigured ModelResponse objects
- records received ModelRequest objects
- can simulate tool calls, final answers, invalid responses, and exceptions

Tests:

- response queue behavior
- request recording
- exhaustion behavior

Do not:

- call a real API in unit tests

## Milestone 10: Agent Runtime Loop

Goal:

- implement the core self-made agent loop

Modules:

- `backend/coding_agent/agent/runtime.py`
- `backend/coding_agent/agent/termination.py`
- `backend/tests/test_agent_loop.py`

Completion criteria:

- user task is added to context
- model is called with messages and tool schemas
- tool calls are executed sequentially
- tool results are appended to context
- final answer finishes successfully
- invalid model responses are handled
- tool failures are returned to the model
- repeated failed tool calls are blocked
- max iterations and max tool calls are enforced
- cancellation is checked between steps
- key events are emitted

Tests:

- success path with mock model
- tool failure recovery path
- repeated failure termination
- max iteration termination
- invalid response termination
- cancellation path

Do not:

- import OpenAI SDK into Agent Runtime
- execute tools directly from Agent Runtime without ToolExecutor

## Milestone 11: CLI Runner

Goal:

- make the first usable product

Module:

- `backend/coding_agent/cli.py`

Completion criteria:

- accepts a workspace path
- accepts a task string
- registers MVP tools
- runs Agent Runtime
- prints event timeline
- prints final answer
- supports mock mode and real-provider mode

Example:

```text
python -m coding_agent.cli --workspace demo/buggy_calculator "fix the failing tests"
```

Do not:

- build a complex TUI
- add Web-only behavior

## Milestone 12: OpenAI-Compatible Provider

Goal:

- connect to a real model without coupling Agent Core to one vendor SDK

Module:

- `backend/coding_agent/models/openai_compatible.py`

Configuration:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

Completion criteria:

- reads configuration from environment
- sends messages and tool schemas
- parses content and tool calls
- handles timeout/API errors
- does not use Agents SDK
- does not use hosted tools

Tests:

- provider parsing with mocked SDK responses
- missing configuration errors

Do not:

- make real API calls in normal unit tests

## Milestone 13: Demo Repository

Goal:

- create a stable demo that proves local coding-agent behavior

Location:

- `demo/buggy_calculator/`

Completion criteria:

- project has real Python code
- project has failing tests
- bug is small and deterministic
- Agent can fix it by reading files, running tests, editing code, and retesting

Do not:

- use a large or flaky demo project

## Milestone 14A: Multi-Turn Agent Core

Goal:

- allow one in-memory `Agent` instance to handle multiple user turns while preserving conversation context

Why now:

- this is more important than JSONL logging or Web UI because it proves the `ContextManager` is useful beyond a single task
- it improves manual testing immediately
- it is a scoring-relevant Agent Core capability, not UI polish

Modules:

- `backend/coding_agent/agent/runtime.py`
- `backend/coding_agent/agent/session.py`
- `backend/tests/test_agent_multiturn.py`

Required behavior:

- add a user message for every new turn
- preserve previous assistant messages and tool results in the same `ContextManager`
- reset per-turn counters before each turn
- reset repeated-tool-call tracking per turn
- reset cancellation state before starting a new turn
- emit events for each turn using the existing event system
- return an `AgentRunResult` for each turn
- allow a new turn after a previous turn completed successfully
- fail cleanly if a new turn is requested while the session is already running

Recommended minimal interface:

```text
Agent.run_turn(user_message: str) -> AgentRunResult
```

Compatibility rule:

- keep `Agent.run(task)` as a backward-compatible alias for one turn, or have it delegate to `run_turn`
- existing CLI tests should continue to pass

Session status recommendation:

- add `IDLE = "idle"` or equivalent if needed
- use `RUNNING` only while a turn is actively executing
- after successful completion, set status to `IDLE` if the session should accept more turns
- the `AgentRunResult.status` for the turn can still be `COMPLETED`

Completion criteria:

- two sequential turns reuse the same context
- the second model request contains messages from the first turn
- a failed tool call in turn 1 does not poison repeated-failure counters in turn 2
- cancellation before a turn prevents that turn only and can be cleared for a later turn
- existing one-shot CLI behavior still works

Tests:

- successful two-turn conversation
- second turn sees previous final answer in context
- per-turn limits reset between turns
- cannot start overlapping turns on the same `Agent`
- `Agent.run` backward compatibility

Do not:

- add persistence
- add Web API
- add JSONL logging
- implement a CLI REPL inside Agent Core
- create a new Agent object for every user turn in the interactive path

## Milestone 14B: Interactive CLI Conversation

Goal:

- expose multi-turn conversation through a simple terminal loop

Module:

- `backend/coding_agent/cli.py`
- `backend/tests/test_cli_runner.py`

CLI behavior:

- keep existing one-shot usage unchanged
- add an explicit flag such as `--interactive`
- in interactive mode, create one `Agent` and reuse it until the user exits
- prompt repeatedly for user input
- run `agent.run_turn(...)` for each non-empty input
- support exit commands: `/exit`, `/quit`
- keep event printing consistent with one-shot mode

Example:

```text
python -m coding_agent.cli --workspace demo/buggy_calculator --provider real --interactive
```

Completion criteria:

- user can say "hello"
- user can ask a follow-up that depends on previous assistant context
- user can ask a coding task after an earlier conversational turn
- one-shot CLI command remains unchanged

Tests:

- interactive mode feeds two inputs and exits
- the same Agent instance is reused
- empty input does not create a model request
- `/exit` exits with code 0

Do not:

- build a full TUI
- add readline/history dependencies
- add async keyboard cancellation yet
- put context logic into CLI

## Milestone 14C: Multi-Turn Documentation and Manual Demo

Goal:

- document how multi-turn mode works and how to demo it

Files:

- `docs/MULTI_TURN_PLAN.md`
- `README.txt`
- `docs/DEMO_PLAN.md`

Completion criteria:

- README explains one-shot mode and interactive mode
- docs explain internal loop vs user-level turns
- demo script includes one short follow-up question before the coding task if time allows

Do not:

- let multi-turn demo replace the main bug-fix demo
- make the 2-minute video depend on long conversation

## Milestone 15: Optional JSONL Event Log

Goal:

- make debugging and video review easier

Module:

- `backend/coding_agent/events/jsonl_store.py`

Completion criteria:

- each AgentEvent is written as one JSON line
- logging failure does not crash the Agent
- event logs are ignored by Git

Do not:

- implement SQLite
- implement replay
- implement session restore

## Milestone 16: README and Demo Script

Goal:

- make the project understandable to evaluators

Files:

- `README.txt`
- `README.md`
- `docs/DEMO_PLAN.md`

Completion criteria:

- explains what the project does
- explains how to install and run
- explains no Agent framework is used
- explains architecture and agent loop
- explains known limitations
- includes a 2-minute demo script

## Milestone 17: Optional FastAPI + SSE

Goal:

- expose Agent Core through a Web API after CLI is stable

Modules:

- `backend/coding_agent/api/app.py`
- `backend/coding_agent/api/routes.py`
- `backend/coding_agent/api/schemas.py`

Completion criteria:

- create session
- start run
- stream events via SSE
- cancel run
- API layer contains no Agent loop logic

Do not:

- implement WebSocket for MVP
- implement multi-user auth
- implement complex session restore

## Milestone 18: Optional Minimal React UI

Goal:

- visualize the agent loop

Location:

- `frontend/`

Completion criteria:

- task input
- run button
- cancel button
- event timeline
- tool call/result display
- final answer display

Do not:

- build a landing page
- build a complex code editor
- put Agent logic in the frontend

## Recommended Commit History

1. `chore: scaffold project structure`
2. `docs: add architecture and implementation rules`
3. `feat: add workspace security abstraction`
4. `feat: add core agent types`
5. `feat: add tool registry and executor`
6. `feat: add filesystem tools`
7. `feat: add replace-in-file tool`
8. `feat: add command execution tool`
9. `feat: add context manager`
10. `feat: add mock model provider`
11. `feat: implement agent runtime loop`
12. `feat: add cli runner`
13. `feat: add openai-compatible provider`
14. `test: add demo repository and integration coverage`
15. `feat: support multi-turn agent sessions`
16. `feat: add interactive cli mode`
17. `docs: polish readme and demo plan`
18. `feat: add jsonl event log`
19. `feat: add fastapi sse api`
20. `feat: add minimal react ui`
