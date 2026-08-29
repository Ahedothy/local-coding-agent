# Multi-Turn Conversation Plan

## Purpose

The current agent can complete one user task per CLI invocation. Inside that one task, it already performs multiple model/tool iterations:

```text
user task -> model -> tool -> result -> model -> ... -> final answer
```

The next feature is user-level multi-turn conversation:

```text
user message 1 -> agent loop -> answer 1
user message 2 -> same session/context -> agent loop -> answer 2
user message 3 -> same session/context -> agent loop -> answer 3
```

This plan is intentionally scoped to in-memory CLI conversation. It does not introduce database persistence, Web API sessions, React state, or any Agent framework.

## Existing Code Facts

Current relevant modules:

- `backend/coding_agent/agent/runtime.py`
- `backend/coding_agent/agent/session.py`
- `backend/coding_agent/context/manager.py`
- `backend/coding_agent/cli.py`
- `backend/coding_agent/models/base.py`
- `backend/coding_agent/tools/executor.py`

Current behavior:

- `Agent.run(task)` appends one user message, runs the iterative loop, emits events, returns `AgentRunResult`, then the CLI process exits.
- `ContextManager` already stores a full sequence of system, user, assistant, and tool messages.
- `Agent` already keeps one `Session`, one `ContextManager`, one `ToolExecutor`, and one `ModelProvider`.
- Runtime counters are reset at the start of `Agent.run`.
- Repeated failed tool-call tracking is currently local to `_run_loop`, which is good for per-run isolation.
- CLI currently creates one agent, runs it once, and exits.

Main gap:

- there is no public turn abstraction that says "run another user message on the same session and context."

## Design Goals

1. Preserve the existing self-written Agent Runtime.
2. Keep Agent Core independent from CLI and future Web UI.
3. Reuse the same `Session` and `ContextManager` across user turns.
4. Reset per-turn execution limits and counters for every new user turn.
5. Keep one-shot CLI usage backward compatible.
6. Make the feature testable with `MockModelProvider`.
7. Avoid persistence until the core behavior is stable.

## Non-Goals

Do not implement these as part of the multi-turn milestone:

- SQLite persistence
- JSONL replay
- FastAPI session restore
- React chat UI
- WebSocket
- full terminal UI
- multi-user support
- command approval workflow
- model-based context summary compaction
- new tools

## Recommended Architecture

```text
Interactive CLI
  -> creates one Agent
  -> reads user input repeatedly
  -> calls Agent.run_turn(user_message)
  -> prints existing AgentEvents
  -> exits on /exit or /quit

Agent.run_turn
  -> validates user message
  -> marks session running
  -> resets per-turn counters
  -> clears previous cancellation flag
  -> appends user message to ContextManager
  -> runs existing internal Agent loop
  -> finalizes this turn
  -> leaves context available for the next turn
```

The important point is that `run_turn` is an Agent Core capability. The CLI loop only supplies input and prints events.

## Proposed Public Interfaces

Keep backward compatibility:

```python
async def run(self, task: str) -> AgentRunResult:
    return await self.run_turn(task)
```

Add the explicit multi-turn method:

```python
async def run_turn(self, user_message: str) -> AgentRunResult:
    ...
```

Optional helper:

```python
def reset_cancellation(self) -> None:
    ...
```

This helper is optional if `run_turn` clears the cancellation event at the start after verifying no turn is currently running.

## Session State

Current statuses:

- `created`
- `running`
- `completed`
- `failed`
- `cancelled`

For multi-turn, a completed turn should not make the session feel closed. The recommended minimal change is to add:

- `idle`

Suggested semantics:

- `CREATED`: session exists but no turn has run yet
- `RUNNING`: one turn is actively executing
- `IDLE`: no turn is running and the session can accept another turn
- `FAILED`: last turn failed, but implementation may still allow another turn if context is not corrupted
- `CANCELLED`: last turn was cancelled, but implementation may still allow another turn after cancellation reset

Do not overbuild a complex session lifecycle. The key requirement is preventing overlapping turns on the same `Agent`.

## Per-Turn State

These values must reset for every user turn:

- iteration count
- total tool calls
- parse error count
- repeated failed tool-call counts
- repeated identical tool-call counts
- task timeout timer
- cancellation flag

These values must persist across turns:

- `session_id`
- workspace root
- model provider
- tool registry/executor
- `ContextManager` messages
- event handler

## Event Behavior

Keep existing event types for MVP. Do not add a large event taxonomy yet.

Recommended payload additions:

- include `turn_index` in event payloads where practical
- keep `iteration` as the per-turn iteration count

Example:

```json
{
  "type": "model_request",
  "iteration": 1,
  "payload": {
    "turn_index": 2,
    "message_count": 8,
    "tool_schema_count": 6
  }
}
```

If adding `turn_index` everywhere creates too much churn, add it only to:

- `user_message`
- `iteration_started`
- `agent_finished`

Do not rename existing event types unless necessary. Existing tests and demo scripts depend on them.

## Context Behavior

`ContextManager` should remain simple:

- keep the system prompt
- append each user turn
- append assistant responses and tool results
- enforce the existing character budget
- truncate old non-system messages when needed

Important:

- Multi-turn makes context truncation more relevant.
- Do not add model-based summary compaction for this milestone.
- If truncation happens, the existing `context_truncated` event is enough.

## Overlapping Turn Protection

The same `Agent` instance should not process two user turns concurrently.

Recommended implementation:

- add an internal `asyncio.Lock`
- `run_turn` acquires the lock
- if the lock is already held, fail fast with a clear error

Fail-fast is preferred over queuing for MVP because it is easier to reason about and test.

Possible result:

```text
AgentRunResult(status=FAILED, error="agent is already running a turn")
```

## CLI Design

Keep one-shot mode:

```text
python -m coding_agent.cli --workspace demo_task_manager --provider real "Fix tests"
```

Add interactive mode:

```text
python -m coding_agent.cli --workspace demo_task_manager --provider real --interactive
```

Recommended parser rules:

- `task` becomes optional only when `--interactive` is present
- without `--interactive`, `task` remains required
- with `--interactive`, any provided `task` can be used as the first turn, but this is optional

Interactive loop behavior:

- print a small startup line with workspace and provider
- read input using `input("> ")`
- ignore blank input
- exit on `/exit` or `/quit`
- call `agent.run_turn(user_input)`
- print final answer or failure message for each turn
- return process code `0` when the loop exits normally

Do not add a dependency for terminal input.

## Testing Plan

Add:

- `backend/tests/test_agent_multiturn.py`

Required tests:

1. Two sequential turns reuse context.
2. Second turn sees previous assistant answer.
3. Tool result from first turn is available to second turn.
4. Per-turn tool-call counters reset.
5. Repeated failed tool-call counters reset.
6. Overlapping turns are rejected cleanly.
7. `Agent.run(task)` still delegates to one turn.

Update:

- `backend/tests/test_cli_runner.py`

Required CLI tests:

1. Existing one-shot tests still pass.
2. `--interactive` accepts two inputs and exits.
3. Blank input does not call the model.
4. `/exit` exits with status code `0`.

Use `MockModelProvider` only. No real API calls in automated tests.

## Implementation Order

### Step 1: Agent Core Turn Method

Modify:

- `backend/coding_agent/agent/runtime.py`
- `backend/coding_agent/agent/session.py`

Work:

- add `run_turn`
- make `run` delegate to `run_turn`
- preserve context between turns
- reset per-turn counters
- protect against overlapping turns
- update session status after a turn

Done when:

- current tests still pass
- new two-turn Agent Core test passes

### Step 2: Multi-Turn Agent Tests

Modify:

- `backend/tests/test_agent_multiturn.py`

Work:

- cover context reuse
- cover counter reset
- cover cancellation reset
- cover overlapping run protection

Done when:

- tests prove the behavior without touching CLI

### Step 3: Interactive CLI Flag

Modify:

- `backend/coding_agent/cli.py`
- `backend/tests/test_cli_runner.py`

Work:

- add `--interactive`
- make `task` optional only for interactive mode
- create one `Agent` and reuse it
- keep event printing unchanged

Done when:

- manual interactive use works
- existing one-shot commands still work

### Step 4: Documentation

Modify:

- `README.txt`
- `docs/DEMO_PLAN.md`

Work:

- explain one-shot vs interactive modes
- add a short manual test script
- keep the main 2-minute demo focused on the stable bug-fix task

Done when:

- a reviewer can run both modes from documented commands

## Manual Test Script

From `backend/`:

```powershell
python -m coding_agent.cli `
  --workspace "C:\SoftwareForProgramming\NJUrecomProject\demo_task_manager" `
  --provider real `
  --interactive
```

Try:

```text
你好，先简单介绍一下你能做什么。
```

Then:

```text
请读取这个项目的文件，找出它是做什么的。
```

Then:

```text
现在修复失败的测试，改动尽量小，并重新运行测试。
```

Exit:

```text
/exit
```

## Risks and Controls

Risk: CLI creates a new Agent for every input.

Control: interactive mode must build the Agent once before the input loop.

Risk: `SessionStatus.COMPLETED` prevents later turns.

Control: introduce `IDLE` or explicitly allow new turns after completed status.

Risk: cancellation remains set after a cancelled turn.

Control: clear cancellation at the beginning of the next turn after checking no turn is active.

Risk: context grows quickly during long interactive sessions.

Control: rely on existing character-budget truncation and tool-result truncation. Do not add summary compaction yet.

Risk: tests become nondeterministic.

Control: use `MockModelProvider` for all automated multi-turn tests.

Risk: feature grows into Web/session persistence.

Control: keep this milestone in-memory and CLI-first.

## Interview Explanation

The key explanation:

> The agent has two levels of "turns." Inside one user turn, the Agent Runtime may call the model and tools many times until it reaches a final answer. User-level multi-turn conversation reuses the same Session and ContextManager across several user turns, so follow-up questions have access to earlier messages and tool results. The CLI only collects input; the Agent Core still owns the loop, tool execution, termination, and context management.

This is a strong interview point because it shows the difference between UI conversation flow and the internal agent orchestration loop.
