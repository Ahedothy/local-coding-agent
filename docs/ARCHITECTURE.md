# Architecture

## Project Goal

`lvyiyou-coding-agent` is a student-built coding agent. It should accept a programming task, interact with a large language model, decide when to use tools, execute those tools locally, update context with tool results, and continue until the task is complete or a termination condition is reached.

The project must demonstrate that the important agent logic is implemented in this repository:

- conversation and context management
- tool definition, registration, discovery, validation, and local execution
- model output normalization and tool call parsing
- agent loop and termination logic
- workspace safety boundaries
- error handling and observability

The MVP should be CLI-first. A Web UI can be added after the Agent Core is stable.

## Non-Negotiable Constraints

Do not use Agent frameworks or Agent SDKs, including LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, CrewAI, or similar libraries that implement the agent loop or tool orchestration.

Allowed:

- official model API clients
- OpenAI-compatible model gateways
- model-native tool calling as a structured output format

Forbidden:

- hosted code execution
- hosted file tools
- Code Interpreter
- using Files API as the agent workspace
- delegating local file or command execution to an API provider

Important distinction:

```text
Provider-native tool calling is only used as a structured model output format.
The model provider never executes tools.
All tool validation, execution, result handling, loop control, and termination are implemented locally.
```

## MVP Architecture

```text
                    +----------------------+
                    |        CLI           |
                    | task input/event log |
                    +----------+-----------+
                               |
                               v
+--------------------------------------------------------------+
|                         Agent Core                           |
|                                                              |
|  +------------------+       +-----------------------------+  |
|  |  Agent Runtime   | ----> |       Context Manager       |  |
|  |  self-made loop  |       | messages + truncation       |  |
|  +--------+---------+       +-----------------------------+  |
|           |                                                  |
|           v                                                  |
|  +------------------+       +-----------------------------+  |
|  |  ModelProvider   | ----> | OpenAI-compatible LLM API   |  |
|  |  adapter layer   |       | structured tool calling     |  |
|  +--------+---------+       +-----------------------------+  |
|           |                                                  |
|           v                                                  |
|  +------------------+       +-----------------------------+  |
|  |  ToolRegistry    | ----> |        ToolExecutor         |  |
|  | tool schemas     |       | validate + local execute    |  |
|  +------------------+       +--------------+--------------+  |
|                                             |                 |
|                                             v                 |
|                                  +-----------------------+    |
|                                  |       Workspace       |    |
|                                  | path/cwd boundaries   |    |
|                                  +-----------------------+    |
|                                                              |
|  +------------------+                                        |
|  |    EventBus      | -> CLI printer / optional JSONL log    |
|  +------------------+                                        |
+--------------------------------------------------------------+

Optional Phase 2:

React UI -> FastAPI -> Agent Core -> SSE Event Stream
```

## Module Responsibilities

### CLI

Responsibilities:

- read a user task
- initialize configuration and workspace
- create a session
- register tools
- start the Agent Runtime
- print structured events and the final answer

Non-responsibilities:

- no agent loop logic
- no model output parsing
- no direct file editing logic beyond passing configuration

### Agent Runtime

Responsibilities:

- own the iterative agent loop
- build model context through ContextManager
- call ModelProvider
- inspect normalized ModelResponse
- execute tool calls through ToolExecutor
- append tool results back into context
- enforce termination conditions
- emit AgentEvents

Non-responsibilities:

- no direct dependency on FastAPI or React
- no direct dependency on OpenAI SDK objects
- no direct file system access except through tools and Workspace

### Context Manager

Responsibilities:

- maintain system, user, assistant, and tool messages
- convert ToolResult values into model-readable tool messages
- enforce a simple character budget
- truncate old messages when necessary

Non-responsibilities:

- no model calls for summary compaction in MVP
- no tool execution
- no provider-specific formatting beyond the internal ModelMessage shape

### ModelProvider

Responsibilities:

- define a provider-neutral interface
- convert internal requests to provider requests
- convert provider responses to internal ModelResponse
- parse provider-native tool calls into internal ToolCall objects

MVP implementations:

- MockModelProvider for deterministic tests
- OpenAICompatibleProvider for real model calls

Non-responsibilities:

- no tool execution
- no loop control
- no context truncation

### ToolRegistry

Responsibilities:

- register Tool instances
- enforce unique tool names
- expose model-visible tool schemas
- look up tools by name

Non-responsibilities:

- no tool execution
- no workspace access

### ToolExecutor

Responsibilities:

- receive internal ToolCall objects
- look up the tool
- validate arguments with the tool's Pydantic model
- execute the tool locally
- catch exceptions
- normalize success and failure into ToolResult
- emit tool events

Non-responsibilities:

- no decision-making about which tool to call
- no direct model calls

### Workspace

Responsibilities:

- store the workspace root
- resolve user-provided paths
- reject path traversal
- reject access outside the workspace
- reject `.git` access
- reject symlink escapes
- validate command working directories
- support file size and binary checks

Non-responsibilities:

- no full operating-system sandbox
- no Docker/container management
- no model interaction

### EventBus

Responsibilities:

- publish structured AgentEvent objects
- support CLI event printing
- optionally write JSONL event logs
- make the agent loop observable for debugging and demo

Non-responsibilities:

- no business logic
- no control flow decisions
- no durable session restore in MVP

## Agent Loop

The core loop is:

```text
User task
  -> add user message
  -> build context
  -> call model
  -> normalize model response
  -> if final answer with no tool calls: finish
  -> if tool calls: execute tools locally, sequentially
  -> append tool results to context
  -> repeat
```

MVP tool execution is sequential, not parallel. If a model returns multiple tool calls in one response, execute at most `max_tool_calls_per_iteration` in order. This avoids dependency and race issues between tools.

Recommended termination defaults:

- `max_iterations = 10`
- `max_total_tool_calls = 25`
- `max_tool_calls_per_iteration = 3`
- `max_consecutive_parse_errors = 2`
- `max_repeated_failed_tool_call = 2`
- `max_same_tool_call = 3`
- `task_timeout_seconds = 300`
- `model_timeout_seconds = 60`
- `command_timeout_seconds = 30`

Successful completion:

- the model returns a non-empty final assistant message and no tool calls

Forced termination:

- max iterations exceeded
- max tool calls exceeded
- repeated failed tool call exceeded
- repeated invalid model responses
- model API timeout after retry
- task timeout
- user cancellation

## Tool System

MVP tools:

- `list_files`
- `read_file`
- `write_file`
- `search_files`
- `execute_command`
- `replace_in_file`

Each tool has:

- name
- description
- Pydantic parameter model
- model-visible JSON schema
- async local execution
- standardized ToolResult

The model sees JSON schema generated from the same Pydantic model used for local validation. This keeps documentation, schema, and runtime validation aligned.

### ToolContext

Tools should receive a ToolContext instead of only a Workspace:

```text
ToolContext
  session_id
  workspace
  config
  cancellation_token
```

This keeps tool execution independent from Agent Runtime while still giving tools access to workspace boundaries and cancellation.

## Workspace Security

MVP must implement:

- resolve every path with `Path.resolve()`
- verify resolved path is inside workspace root
- reject `../` traversal
- reject absolute paths outside workspace
- reject symlink escapes
- reject `.git` access
- reject binary file reads/edits
- limit file read and write size
- run commands only with cwd inside workspace
- run commands with `shell=False`
- accept command as `list[str]`, not shell string
- enforce command timeout
- truncate stdout and stderr
- block obviously dangerous commands as defense in depth

This is not a complete security sandbox. It is a reasonable, explainable safety boundary for a student project.

## Context Management

MVP context management should be simple:

- always keep the system prompt
- keep the latest user task
- keep recent assistant and tool messages
- truncate old non-system messages when context exceeds a character budget
- truncate long tool outputs before adding them to context

Do not implement summary compaction in MVP. It requires extra model calls and creates avoidable instability. Use the event name `context_truncated`, not `context_compacted`.

## Events

Required event types:

- `session_started`
- `user_message`
- `iteration_started`
- `model_request`
- `model_response`
- `tool_started`
- `tool_finished`
- `tool_failed`
- `assistant_message`
- `context_truncated`
- `agent_finished`
- `agent_error`

Events are important because they make the agent loop visible in CLI output, Web UI streaming, JSONL logs, tests, and the final demo video.

## CLI and Web Strategy

MVP:

```text
CLI -> Agent Core
```

Phase 2:

```text
React -> FastAPI -> Agent Core -> SSE
```

SSE is preferred over WebSocket for Phase 2 because agent events are naturally server-to-client. Starting and cancelling tasks can use normal HTTP requests. WebSocket is unnecessary for MVP.

## Architecture Decisions

### Python 3.12+

Chosen because Python is strong for file operations, subprocess management, asyncio, Pydantic, FastAPI, and pytest.

### FastAPI

Chosen only for Phase 2 Web API. It must wrap Agent Core, not contain Agent Core.

### React + TypeScript + Vite

Chosen only for optional Phase 2 UI. The UI should visualize events, not implement agent logic.

### SSE

Chosen for one-way event streaming. WebSocket is not needed for MVP.

### JSONL

Chosen as optional event logging. SQLite is intentionally not part of MVP.

### `replace_in_file`

Chosen instead of full `apply_patch` for MVP because it is stable, testable, and enough to demonstrate local code editing. Full unified diff support is a future enhancement.

## Final MVP Statement

The final MVP is a CLI-first Python coding agent with a self-implemented Agent Runtime, ToolRegistry, ToolExecutor, ContextManager, Workspace safety boundary, ModelProvider abstraction, and EventBus. It uses OpenAI-compatible tool calling only to receive structured tool intentions from the model. All tools execute locally inside the configured workspace.
