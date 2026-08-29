# Test Plan

## Testing Philosophy

The Agent loop must be tested without calling a real LLM. Real model calls are useful for manual demos, but they are too slow, expensive, and nondeterministic for unit tests.

The test suite should prove:

- tools execute locally
- workspace security cannot be bypassed through normal tool inputs
- model tool calls are parsed into local ToolCall objects
- tool results are returned into context
- the Agent loop terminates correctly
- repeated failures do not cause infinite loops

## Test Pyramid

```text
Manual real-model demo
Integration tests with MockModelProvider
Agent loop tests
Context tests
ToolExecutor tests
Tool unit tests
Workspace security tests
```

Most tests should be fast unit tests. Only the manual demo should require a real API key.

## 1. Workspace Security Tests

Goal:

- prove the workspace boundary works before tools depend on it

Required scenarios:

- resolving `src/file.py` inside workspace succeeds
- resolving `./src/file.py` succeeds
- resolving `../secret.txt` fails
- resolving an absolute path outside workspace fails
- resolving a symlink that points outside workspace fails
- accessing `.git/config` fails
- command cwd inside workspace succeeds
- command cwd outside workspace fails

Expected result:

- safe paths return resolved absolute paths
- unsafe paths raise a workspace/security error

## 2. Tool Unit Tests

### `list_files`

Scenarios:

- lists files in a directory
- lists recursively when requested
- limits max entries
- rejects path outside workspace
- rejects file path when directory expected

### `read_file`

Scenarios:

- reads a text file
- reads a line range
- truncates large files
- rejects binary files
- rejects directories
- rejects workspace escape

### `write_file`

Scenarios:

- creates a new file
- overwrites an existing file when allowed
- rejects overwrite when `overwrite=False`
- creates parent directories only when allowed
- rejects writes outside workspace
- rejects writes under `.git`
- rejects content over max size

### `search_files`

Scenarios:

- finds matching text
- supports regex matching
- supports case-insensitive literal matching
- returns bounded before/after context lines
- truncates long matching lines without losing the match
- respects path scope
- respects optional glob
- truncates results at max matches
- skips binary and very large files
- rejects empty query
- rejects invalid regex
- rejects workspace escape

### `replace_in_file`

Scenarios:

- replaces one exact text match
- fails when `old_text` is missing
- fails when multiple matches exist and expected replacements is `1`
- succeeds when expected replacement count matches
- rejects binary files
- rejects workspace escape

### `apply_patch`

Scenarios:

- applies standard unified diff hunks to one file
- applies a standard diff to multiple files
- applies multiple ordered hunks to one file
- dry-runs a patch and returns the generated diff without writing files
- rejects malformed diff headers and hunk line counts
- rejects stale hunk context without changing any file
- rejects binary files and workspace escapes
- rolls back earlier writes when a later file write fails
- returns the generated standard unified diff in the tool output

### `execute_command`

Scenarios:

- runs a successful command
- returns non-zero exit code for a failing command
- marks non-zero exit code as `success=false` and emits a failed tool event
- reports a missing executable with a structured start failure
- kills timed-out command
- truncates stdout and stderr
- rejects cwd outside workspace
- rejects empty command
- rejects blocked dangerous command
- uses `shell=False`

### Priority 6 inspection tools

#### `get_file_info`

Scenarios:

- reports text file size, UTC modification time, type, MIME type, encoding,
  confidence, and line count
- detects BOM-based UTF-16/UTF-32 and common legacy text encodings
- reports binary magic types without attempting to decode them as text
- reports directory metadata with a directory type and no line count
- marks byte scanning and line counting as truncated for large files
- exposes the detection method so heuristic results are distinguishable
- rejects missing paths, workspace escapes, and `.git` paths

#### `list_directory_tree`

Scenarios:

- returns deterministic directory-first entries and a readable tree
- supports a scoped starting directory
- respects `max_depth` and reports depth truncation
- respects `max_entries` and reports entry truncation
- skips `.git`, `node_modules`, `__pycache__`, build, and distribution output
- skips symlinks and rejects paths outside the Workspace
- rejects a file path when a directory is required

#### `git_diff`

Scenarios:

- reports tracked modifications as a unified diff
- reports untracked files in structured Git status
- supports a path scope inside the Workspace
- truncates oversized diff or status output and reports the relevant
  `diff_truncated` or `status_truncated` flag
- returns a structured failure for a non-Git directory
- returns a structured failure when Git is unavailable or times out
- does not expose `.git` metadata or provide Git mutation operations
- rejects workspace escapes before starting a subprocess

#### `inspect_environment`

Scenarios:

- reports platform and Python information without exposing secret values
- reports fixed runtime, compiler, build-tool, and package-manager probes
- preserves per-probe failures when an executable is missing or cannot start
- detects project marker files and inferred ecosystems in the selected directory
- reports an empty project marker list for an empty workspace
- checks only bounded localhost ports and returns structured availability
- rejects duplicate or out-of-range ports through Pydantic validation
- rejects a file path and workspace escapes before inspecting project markers
- never accepts or executes an arbitrary command

### Priority 4A local process management

#### `manage_process`

Scenarios:

- starts a local process with an argument array and returns a process handle
- keeps a long-running process responsive while stdout and stderr are drained
- lists managed process status without returning unbounded output
- reads bounded output and supports clearing the returned buffer
- writes explicit stdin text to a running process
- reports a normal exited process and its return code
- stops a process gracefully and supports bounded forced cleanup
- rejects shell strings, blocked executables, workspace escapes, and missing handles
- applies approval only to `start`, `write`, and `stop`, not to `list`, `status`, or `read`
- caps active/retained process records and per-stream output buffers

## 3. ToolRegistry and ToolExecutor Tests

Goal:

- prove generic tool orchestration works independently of Agent Runtime

Scenarios:

- register a tool
- reject duplicate tool names
- export model-visible schema
- execute a valid mock tool call
- reject unknown tool name
- reject invalid arguments
- convert tool exception to failed ToolResult
- pass ToolContext to the tool

Expected result:

- ToolExecutor never leaks raw exceptions to Agent Runtime for expected tool failures
- all tool outputs are normalized into ToolResult

## 4. ContextManager Tests

Goal:

- prove model context is constructed and bounded correctly

Scenarios:

- system prompt is always included
- user message is appended
- assistant message is appended
- tool result is converted to a tool message
- tool output is truncated before insertion
- old messages are removed when char budget is exceeded
- latest user task is preserved

Important assertion:

- MVP should not call a model to summarize context

## 5. MockModelProvider Tests

Goal:

- make LLM behavior deterministic in tests

Scenarios:

- returns queued ModelResponse objects in order
- records ModelRequest objects
- simulates a tool call response
- simulates a final answer response
- simulates an invalid response
- simulates a provider exception

Expected result:

- no test in this category requires network access or API keys

## 6. Agent Loop Tests

Goal:

- prove the project implements its own agent loop

Required scenarios:

### Success path

Mock model responses:

1. returns a `read_file` ToolCall
2. returns a final answer

Assertions:

- model is called twice
- tool executes locally once
- tool result is added to context
- final answer finishes the run
- events are emitted in expected order

### Multi-tool path

Mock model responses:

1. returns `list_files`
2. returns `read_file`
3. returns `execute_command`
4. returns `replace_in_file`
5. returns `execute_command`
6. returns final answer

Assertions:

- tools execute sequentially
- context grows with tool results
- final status is success

### Tool failure recovery

Mock model responses:

1. returns invalid `read_file` path
2. receives tool error in context
3. returns corrected `list_files`
4. returns final answer

Assertions:

- failed ToolResult is visible in context
- Agent does not crash on tool failure

### Repeated failure termination

Mock model repeatedly calls the same failing tool with the same arguments.

Assertions:

- Agent blocks repeated failed call
- Agent terminates after configured threshold

### Invalid model response

Mock model returns:

- empty content and no tool calls
- malformed tool arguments

Assertions:

- Agent adds a correction or error event
- Agent terminates after repeated parse errors

### Max iteration termination

Mock model keeps producing non-final tool calls.

Assertions:

- Agent stops at `max_iterations`
- status is failure
- error reason is clear

### Cancellation

Simulate cancellation before a model call or before a tool call.

Assertions:

- Agent stops cleanly
- emits cancelled/finished event
- does not continue executing tools

### Planning and Reflection

Mock model responses:

1. returns assistant text plus a tool call
2. returns assistant text plus another tool call
3. returns a final answer

Assertions:

- the first tool-producing response emits `plan`
- the later tool-producing response emits `reflection`
- plan text remains in the assistant message sent into the next request
- a direct final answer emits neither event
- no extra model request is created solely for planning

## 7. Multi-Turn Conversation Tests

Goal:

- prove user-level conversation works without moving Agent logic into CLI or Web UI

Important distinction:

- internal multi-iteration tool loop is already tested by Agent loop tests
- user-level multi-turn means multiple user inputs share the same `Session` and `ContextManager`

Required scenarios:

### Two successful user turns

Mock model responses:

1. first turn returns a final answer
2. second turn returns a final answer

Assertions:

- both calls return successful `AgentRunResult`
- provider receives two separate `ModelRequest` objects
- second request contains the first user message and first assistant answer
- session id remains the same

### Follow-up uses previous context

Mock model responses:

1. first turn reads a file and answers
2. second turn answers a follow-up without re-reading the same file

Assertions:

- first turn includes tool execution
- second turn request contains prior tool result
- no new tool call is required for the follow-up when the mock response is final text

### Per-turn limits reset

Mock model responses:

1. first turn uses tool calls near the configured limit and completes
2. second turn still has a fresh per-turn tool budget

Assertions:

- second turn is not rejected because of first-turn counters
- repeated failed tool-call counters start fresh for each turn

### Overlapping run protection

Start a turn and attempt to start another turn on the same `Agent` before the first completes.

Assertions:

- second turn fails cleanly or raises a clear local error
- context is not corrupted

### Cancellation scope

Cancel one running turn, then clear or reset cancellation for the next turn.

Assertions:

- cancelled turn stops cleanly
- later turn can still run when cancellation is reset by the public API

### Backward compatibility

Call existing `Agent.run(task)`.

Assertions:

- it still behaves like a one-shot turn
- existing CLI tests continue to pass

## 8. Interactive CLI Tests

Only required after interactive CLI mode is implemented.

Scenarios:

- `--interactive` starts a loop and accepts two user inputs
- `/exit` or `/quit` exits successfully
- blank input is ignored
- the same `Agent` instance handles all inputs
- one-shot mode still accepts a task argument and exits after one turn

Implementation hint:

- test by monkeypatching standard input rather than calling a real model
- use `MockModelProvider` for deterministic responses

## 9. Integration Test with Mock LLM

Goal:

- test a realistic task without real model nondeterminism

Setup:

- create a temporary copy of the demo project
- use MockModelProvider with a scripted sequence:
  - list files
  - read source
  - read tests
  - run failing tests
  - replace text
  - run passing tests
  - final answer

Assertions:

- the file is actually modified
- test command actually runs locally
- final command exit code is 0
- event sequence contains tool start and finish events
- Agent finishes successfully

## 10. Trace Summary and Replay Tests

Goal:

- prove that a recorded JSONL observability trace can be inspected after completion

Required scenarios:

- load a valid JSONL fixture into `AgentEvent` objects
- summarize session id, workspace, tasks, status, iterations, and tool calls
- aggregate model usage when numeric usage fields are present
- report command return codes and per-tool durations
- render a readable summary and chronological timeline
- support JSON output for machine-readable inspection
- reject malformed JSONL with a line-specific error
- handle an empty event sequence safely
- never execute a model or local tool while tracing

## 11. API/SSE Tests

Only required if FastAPI Phase 2 is implemented.

Scenarios:

- create session
- start run
- receive SSE events
- cancel run
- API layer does not bypass Agent Core

Do not test real model calls here.

## 12. Manual Real-Model Demo

Goal:

- prove the agent works with a real model provider

Requirements:

- API key configured through environment variables
- demo workspace reset to failing state
- command run from README instructions
- final tests pass

This is not a normal automated test. It is a manual verification step before recording the video.

## Definition of Done for Testing

Before the project is considered demo-ready:

- all unit tests pass
- Agent loop tests pass with MockModelProvider
- workspace security tests pass
- integration test with mock model passes
- multi-turn tests pass if Milestone 14A/14B is implemented
- manual real-model demo has succeeded at least three times
- no tests require hardcoded secrets
