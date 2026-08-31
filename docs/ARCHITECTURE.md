# Architecture

> This document began as an MVP design proposal. Sections that conflict with
> the current implementation are historical. The current system includes
> SQLite history, deterministic context compaction, verification evidence,
> optimistic concurrency, AGENTS.md instructions, and bounded model retries.

## 当前实现摘要

本项目是一个由 Python 自行实现的本地 Coding Agent。用户提交任务后，
Agent 通过模型生成结构化工具调用，由本地 `ToolExecutor` 完成参数校验、
审批和执行，再把结果放回上下文，循环直到得到最终答复或触发安全终止条件。

当前主要模块如下：

- `Agent Runtime`：模型调用、工具循环、重试、取消、超时和终止条件；
- `ContextManager`：工具结果限长、旧结果压缩、事实摘要和消息组删除；
- `Workspace`：路径边界、符号链接、文本/二进制和命令工作目录校验；
- `ToolRegistry / ToolExecutor`：工具 schema、参数验证、审批和本地执行；
- `VerificationLedger`：记录测试、构建、Lint、类型检查和 Smoke Test 等证据；
- `ChangeStore`：保存本轮修改快照，并在撤销前检查文件是否被后续修改；
- `SQLite / SSE / JSONL`：分别提供历史恢复、Web 事件流和离线诊断；
- `AGENTS.md`：读取工作区根目录项目指令，并限长注入模型上下文。


## Project Goal

`Local Coding Agent` is a student-built coding agent. It should accept a programming task, interact with a large language model, decide when to use tools, execute those tools locally, update context with tool results, and continue until the task is complete or a termination condition is reached.

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

- no model calls are required for summary compaction; the implementation uses
  deterministic extraction and compacted tool-result payloads
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
- no restoration of live Agent state during trace replay; JSONL is only the
  optional append-only observability format, while Web history is stored in SQLite

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

## User-Level Multi-Turn Conversation

The current MVP already has an internal multi-iteration agent loop: one user task can lead to many model calls, tool calls, tool results, and a final answer.

The next important capability is user-level multi-turn conversation:

```text
user turn 1 -> agent loop -> final answer
user turn 2 -> same Session + same ContextManager -> agent loop -> final answer
user turn 3 -> same Session + same ContextManager -> agent loop -> final answer
```

This is different from the existing one-task CLI process. Multi-turn support must be implemented in Agent Core first, then exposed through an interactive CLI.

Required design rules:

- Keep one `Session`, one `ContextManager`, one `ToolExecutor`, and one `ModelProvider` for the whole conversation.
- Each user turn gets its own run counters: iterations, tool calls, repeated failed tool calls, parse errors, and timeout.
- Conversation history remains in `ContextManager` across turns, so the model can refer to earlier user instructions, prior tool results, and previous assistant answers.
- A completed turn must not permanently mark the whole session as unusable. The session may return to an idle/waiting state after a completed turn.
- Cancellation applies to the currently running turn, not to the entire conversation object.
- Interactive CLI is only a transport wrapper. It must not implement the agent loop, tool routing, context truncation, or model parsing.

Multi-turn conversation state remains in memory during normal execution. The Web
API additionally persists completed and partially recorded runs in a local
SQLite store. Each run has its own bounded `Session`/`ContextManager` snapshot,
so continuing an older run restores that exact conversation point instead of
silently using a newer session snapshot. Replay still only parses recorded
events and never restores live tool execution or approval futures.

## Tool System

Core tools:

- `list_files`
- `read_file`
- `write_file`
- `search_files`
- `manage_process`
- `execute_command`
- `replace_in_file`
- `apply_patch`

Read-only inspection tools:

- `get_file_info`
- `list_directory_tree`
- `git_diff`

Environment detection:

- `inspect_environment`

The inspection tools are intentionally read-only. They give the model compact
metadata, structure, and change awareness without adding Git mutation,
package installation, network browsing, or formatter-specific execution.
`execute_command` remains the single local command path, including for test
commands, so command safety and result semantics do not diverge across tools.
`search_files` is a local UTF-8 code-search tool. It supports literal matching
by default plus opt-in regular expressions, case-insensitive matching, glob
filtering, scoped traversal, bounded match counts, small context windows, and
line excerpts that prevent a single long line from bloating model context.

Each tool has:

- name
- description
- Pydantic parameter model
- model-visible JSON schema
- async local execution
- standardized ToolResult

The model sees JSON schema generated from the same Pydantic model used for local validation. This keeps documentation, schema, and runtime validation aligned.

### Inspection Tool Contracts

Inspection tools follow the same `Tool` and `ToolExecutor` contract as file
editing and command tools:

- `get_file_info` scans at most a bounded number of bytes and reports file
  category/type, MIME type, binary status, best-effort encoding, confidence,
  detection method, and whether line scanning was truncated
- `list_directory_tree` applies deterministic ignore rules, skips symlinks,
  and caps both traversal depth and returned entries
- `git_diff` executes only read-only `git diff` and `git status` subprocesses
  with `shell=False`, a fixed timeout, and one shared bounded diff/status
  payload

Every requested path still goes through `Workspace`, and failures are returned
as structured `ToolResult` values by the generic executor. The model receives
the same Pydantic-generated schemas that local validation uses.

### Environment Detection Contract

`inspect_environment` is a read-only local capability for deciding how to work
in an unfamiliar project. It reports platform information, fixed probes for
common runtimes, compilers, build tools, and package managers, project marker
files in a selected workspace directory, whether a small allowlist of safe
configuration variables is present, and whether requested localhost ports can
be bound.

The tool never accepts an arbitrary command, never installs packages, never
returns environment variable values, and never modifies the workspace. Version
probes use `shell=False`, bounded timeouts, and a fixed argument list. Port
checks only bind to `127.0.0.1` and are treated as a point-in-time availability
hint rather than a reservation. Probe failures are represented per item so a
missing compiler does not make the whole environment inspection unusable.

`get_file_info` uses only Python standard-library detection. It gives BOM-based
results high confidence, recognizes common binary magic bytes and extensions,
and tries a small set of common text encodings such as UTF-8 and GB18030 when
there is no BOM. Legacy-encoding results are explicitly marked heuristic; the
tool never silently rewrites a file or claims universal charset detection.

### Local Process Management Contract

`manage_process` provides a small lifecycle protocol for local development
servers and interactive command-line programs that cannot be handled by a
single blocking `execute_command` call. The supported operations are
`start`, `list`, `status`, `read`, `write`, and `stop`.

`start` accepts only an argument array and validates its working directory
through `Workspace`. It launches with `shell=False`, captures stdout and stderr
through background readers, and returns an opaque process handle. `read` and
`status` return bounded output tails; `write` sends explicit UTF-8 text to the
child stdin; `stop` requests termination and escalates to kill after a bounded
grace period. Side-effecting operations require the existing approval gate,
while inspection operations remain read-only. The process table is kept by the
tool instance for the lifetime of the Agent session, and finished records are
bounded so a long conversation cannot grow memory without limit.

The tool does not provide arbitrary shell parsing, package installation,
detached system services, process tree ownership, or cross-session process
restoration. This keeps the feature useful for local coding workflows while
remaining explicit and explainable under the assignment constraints.

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

This is not a complete security sandbox. The Workspace boundary applies to
direct file operations and to the command working directory; it does not
restrict every file or system resource reachable by a child process. A command
still runs with the current user's permissions. `shell=False`, argument-array
invocation, approval, timeouts, output limits, and obvious-executable blocking
reduce accidental and shell-injection risk, but they do not replace an OS or
container sandbox. This is a reasonable, explainable safety boundary for a
local student project, and the limitation must be disclosed to users.

## Context Management

MVP context management should be simple:

- always keep the system prompt
- keep the latest user task
- keep recent assistant and tool messages
- truncate old non-system messages when context exceeds a character budget
- truncate long tool outputs before adding them to context

Historical note: the original MVP proposal deferred summary compaction. The
current implementation uses deterministic summary compaction and emits both
`context_truncated` and `context_compacted` events as appropriate.

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
- `plan`
- `reflection`
- `context_truncated`
- `agent_finished`
- `agent_error`

Events are important because they make the agent loop visible in CLI output, Web UI streaming, JSONL logs, tests, and the final demo video.

### Planning and Reflection

Planning is an optional protocol around the existing model loop, not a second
planner or Agent. When a model response contains both assistant text and tool
calls, the first such response in a turn is emitted as `plan`; later such
responses are emitted as `reflection`. The same text remains the normal
assistant message sent back into ContextManager. Simple final-answer turns do
not require a plan, and the runtime never performs an extra model call to
generate one.

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

### JSONL and trace replay

JSONL remains the append-only event format for CLI logs and trace replay. The
Web API uses a local SQLite run store for indexed run metadata, complete event
streams, and bounded session snapshots. The store keeps both the latest
session snapshot and an exact snapshot per run. The `coding_agent.trace` module
  and the Web UI replay reader parse recorded data without executing tools.
The history list is session-oriented: several per-turn run records are grouped
into one conversation entry, while each run remains independently replayable
and resumable at its own snapshot. Loading a replay does not switch or scan
the current workspace; only an explicit continue action restores the recorded
workspace. A session starts with the display title `New conversation`; its
first user message produces one stable local title by whitespace normalization
and truncation. The title is stored as session metadata and is never
overwritten by later turns. Session identity always comes from `session_id`,
never from the display title, so identical titles remain separate
conversations.
Replay is read-only; an explicit continue action restores the selected run's
workspace, context, and turn counter, then starts a new turn without resuming
an old tool call or approval.

### Editing tools

`replace_in_file` remains the small, reliable path for one exact replacement.
`apply_patch` accepts standard unified diff text for related multi-line or
multi-file edits. Parsing, context validation, Workspace checks, atomic writes,
rollback, and diff reporting are implemented locally in the tool layer; no
shell patch command or agent framework is involved. `apply_patch` also supports
`dry_run=true`, which performs the same local validation and returns the
generated standard diff without writing files, requesting edit approval, or
creating an undo record.

## Final MVP Statement

The final MVP is a CLI-first Python coding agent with a self-implemented Agent Runtime, ToolRegistry, ToolExecutor, ContextManager, Workspace safety boundary, ModelProvider abstraction, and EventBus. It uses OpenAI-compatible tool calling only to receive structured tool intentions from the model. All tools execute locally inside the configured workspace.
