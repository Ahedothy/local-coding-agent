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
- respects path scope
- respects optional glob
- truncates results at max matches
- skips binary and very large files
- rejects empty query
- rejects workspace escape

### `replace_in_file`

Scenarios:

- replaces one exact text match
- fails when `old_text` is missing
- fails when multiple matches exist and expected replacements is `1`
- succeeds when expected replacement count matches
- rejects binary files
- rejects workspace escape

### `execute_command`

Scenarios:

- runs a successful command
- returns non-zero exit code for a failing command
- kills timed-out command
- truncates stdout and stderr
- rejects cwd outside workspace
- rejects empty command
- rejects blocked dangerous command
- uses `shell=False`

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

## 7. Integration Test with Mock LLM

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

## 8. API/SSE Tests

Only required if FastAPI Phase 2 is implemented.

Scenarios:

- create session
- start run
- receive SSE events
- cancel run
- API layer does not bypass Agent Core

Do not test real model calls here.

## 9. Manual Real-Model Demo

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
- manual real-model demo has succeeded at least three times
- no tests require hardcoded secrets
