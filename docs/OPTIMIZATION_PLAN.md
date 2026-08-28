# Optimization Plan

## Purpose

This document records the recommended improvement plan after reviewing the assignment description and the current implementation. The goal is not to turn the project into a full commercial agent platform. The goal is to make the submitted project look complete, credible, and technically strong while staying fully inside the assignment rules.

The project must continue to be a self-implemented coding agent:

- no LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, CrewAI, or similar agent framework
- model-native tool calling may be used only as structured model output
- all file reading, file writing, command execution, context management, loop control, and termination logic must remain implemented locally in this repository
- no hosted code execution, Code Interpreter, or provider-hosted file tools
- no API keys or credentials in Git, README, logs, screenshots, or demo video

## Current Baseline

The current codebase already has a solid MVP:

- self-written `Agent` runtime loop with iterations, tool calls, termination limits, cancellation, and events
- provider-neutral model types and an OpenAI-compatible adapter
- deterministic `MockModelProvider` for tests
- local tool registry and executor with Pydantic validation
- local filesystem, exact replacement, and command tools
- workspace path boundary, `.git` blocking, symlink escape protection, command cwd validation, command timeout, and output truncation
- bounded in-memory context manager
- multi-turn in-memory sessions through CLI and Web API
- optional JSONL event logging
- FastAPI + SSE API layer
- React UI that visualizes the event stream
- strong automated coverage: `90 passed, 1 skipped` when pytest uses a writable repo-local temp directory

Because the base is already working, the next phase should avoid destabilizing the core. The best improvements are the ones that make the agent easier to trust, easier to demo, and more visibly agent-like.

## Prioritization Rules

Use these rules when deciding what to implement before the deadline:

1. Prefer core agent capability over UI decoration.
2. Prefer explainable, locally implemented features over impressive but risky integrations.
3. Prefer features that can be demonstrated in a two-minute video.
4. Prefer changes that can be tested deterministically with `MockModelProvider`.
5. Avoid changes that could blur the assignment boundary, especially anything that looks like using an agent framework or hosted execution.

## Priority 0: Submission Hardening

Why this comes first:

The assignment explicitly requires a Git repository, a short `README.txt`, and a two-minute MP4 demo. A technically good project can still lose points if the submission is confusing, too long, or accidentally leaks credentials.

Tasks:

- Keep `README.txt` within the required length and make it evaluator-facing rather than developer-facing.
- Put the repository URL in `README.txt`.
- Add a short "What is self-implemented" section that names the local runtime, tools, context manager, workspace boundary, and event stream.
- Add a short "What is not used" section that explicitly says no Agent framework, no Agent SDK, no hosted code execution, and no hosted file tools are used.
- Add a "How to run the demo" section with exact CLI and Web UI commands.
- Add a "Known limitations" section so limitations look intentional rather than hidden.
- Verify `.env` is ignored and never referenced with real values.
- Rehearse and record the two-minute demo with the failing-test-to-passing-test flow.

Acceptance criteria:

- `README.txt` is concise, accurate, and submission-ready.
- The demo video shows local tool calls, a failing test, a local edit, a passing test, and the final answer.
- No API key appears in repository files, terminal output, event logs, browser UI, or the video.

Recommended effort:

- Very high value, low implementation risk.

## Priority 1: Context Compression Upgrade

### Status: Implemented

The first implementation of this priority is complete. The context manager now
provides deterministic local compression without an additional model call or
agent framework:

- `ContextStats` reports total and maximum characters, message counts, tool
  result size, summary size, utilization, and compaction count.
- Older completed message groups are converted into a structured
  `[conversation memory]` message containing goals, files, assistant notes,
  command results, and unresolved tool errors.
- The system prompt, latest user message, recent message groups, and complete
  assistant-tool groups are preserved where the configured budget permits.
- A very small budget still uses the original group-aware drop fallback, so an
  orphan tool result cannot be sent to the model.
- `context_compacted` and `context_truncated` events expose before/after sizes,
  strategy, compacted message count, and current context statistics.
- `model_request` events also include the context statistics used for that
  request, and the Web UI shows current budget usage and compaction count.

Verification: `python -m pytest --basetemp .pytest-context-check` passes with
`93 passed, 1 skipped` after adding focused tests for summary retention,
statistics, and tool-call grouping. The skipped test only covers symlink
creation unavailable in the current Windows test environment.

### Related Verification Hardening

The demo path also now treats command execution as evidence rather than a
best-effort hint. `execute_command` reports non-zero exit codes as
`success=false`, returns a structured failure when an executable cannot be
started, and resolves workspace-local Windows executables with an explicit
relative path when possible. The system prompt tells the model to require
`success=true`, `returncode=0`, and expected output before claiming a test or
program run succeeded.

Why this is high priority:

The current `ContextManager` uses a simple character budget and drops older message groups. That is acceptable for MVP, but context compression is one of the most natural places to make the agent feel more advanced. It is also easy to explain in an interview: the agent preserves useful memory instead of blindly keeping or dropping raw history.

Recommended design:

- Keep the current simple truncation path as the default fallback.
- Add an optional local `ConversationMemory` or `ContextCompactor` layer.
- When the context exceeds a configurable threshold, compact older completed turns into a short structured summary.
- Preserve these items exactly, without summarizing them away:
  - system prompt
  - latest user message
  - currently open assistant tool calls and matching tool results
  - recent N messages
  - durable memory summary
- The summary should contain:
  - user goals and constraints
  - files already inspected
  - edits already made
  - commands already run and outcomes
  - unresolved errors or next steps
- Emit a new event such as `context_compacted` with before/after character counts.

Important assignment boundary:

If model-based summarization is used, it must only summarize conversation history. It must not execute tools, read files, or control the agent loop. A safer first version is deterministic extractive compression implemented locally.

Suggested implementation order:

1. Add a `ContextStats` data model with total characters, message count, tool result characters, and compacted summary characters.
2. Add deterministic compression for old tool results and old assistant messages.
3. Add a memory summary message inserted after the system prompt.
4. Add `context_compacted` event payload fields: `before_chars`, `after_chars`, `messages_compacted`, and `strategy`.
5. Add tests for preserving valid assistant/tool-call grouping after compression.
6. Add Web UI display for context budget and compaction events.

Acceptance criteria:

- Multi-turn conversations keep useful prior goals after old raw messages are removed.
- Tool-call messages are never split from their required tool-result messages.
- The agent can run a long mock multi-turn test without exceeding the configured budget.
- The event stream clearly shows when context was compacted.

Recommended effort:

- High value, medium risk. Implement after submission hardening, before cosmetic UI work.

## Priority 2: Stronger Editing Tooling

### Status: Implemented

The local editing toolset now includes `apply_patch` alongside the original
`replace_in_file` tool. `apply_patch` accepts a structured JSON argument with a
standard unified diff string containing `---`/`+++` file headers and `@@`
hunk headers. It validates every touched path through `Workspace`, rejects
binary and over-sized files, checks all hunks in memory before any write, and
uses same-directory atomic replacement for each file.

Multi-file patches are transactional at the tool level: if validation fails,
no file is changed; if a later write fails, already-written files are restored.
Successful results include touched files, hunk counts, line and byte deltas,
a concise diff summary, and the generated standard unified diff. The same diff
is included in the tool-finished event so it is visible in CLI and Web UI
output. Failed results include rejected hunk diagnostics and rollback status.
The system prompt tells the model to use
`replace_in_file` for one simple replacement and `apply_patch` for related
multi-line or multi-file edits.

Focused coverage verifies multiple files, ordered hunks, stale context,
workspace escapes, binary rejection, and write-failure rollback.

Why this matters:

The current `replace_in_file` tool is safe and easy to test, but it is limited. Real coding agents usually need patch-style edits. Adding a locally implemented patch tool would make the project look more capable while still staying within the assignment rules.

Recommended design:

- Keep `replace_in_file` as the simple reliable edit tool.
- Add a new `apply_patch` or `apply_unified_diff` local tool implemented in this repository.
- Accept a structured patch format rather than arbitrary shell commands.
- Validate every touched path through `Workspace`.
- Reject binary files and files above the size limit.
- Apply hunks only when context matches.
- Return a structured result with touched files, applied hunks, rejected hunks, and a concise diff summary.

Suggested implementation order:

1. Define the patch argument schema.
2. Implement parsing and validation in a small isolated module.
3. Apply one file at a time with exact context checks.
4. Add rollback-on-failure for multi-file patches.
5. Add tests for successful patch, stale context, path escape, binary rejection, and partial failure rollback.
6. Update system prompt and tool descriptions so the model knows when to use exact replacement vs patch.

Acceptance criteria:

- The agent can make multi-line edits without replacing a huge block manually.
- A malformed or stale patch fails safely without corrupting files.
- Tests cover both success and failure paths.

Recommended effort:

- High value, medium-high risk. Do this only after context work or if demo tasks need richer edits.

## Priority 3: Web UI Upgrade

### Status: Implemented

The Web UI now presents an operational run console centered on the Agent loop:

- events are grouped by iteration rather than shown as one undifferentiated list
- the run header shows status, run id, duration, iterations, tool calls, failures,
  and context budget utilization
- event filters cover All, Model, Tools, Context, and Errors
- selecting an event opens a payload inspector with a copy action
- assistant final answers, active tools, context compaction, empty states, and
  failure states have dedicated visual treatments
- the layout remains usable on narrow screens by stacking the inspector below
  the event list
- the main shell uses a light three-pane layout with a collapsible workspace
  tree and recent-task history on the left, the conversation/run monitor in the
  center, and file preview on the right
- the local API server opens a native directory picker on the same machine and
  returns the selected path; subsequent Agent tools operate on that original
  directory, with no browser upload or temporary copy

The Workspace API now returns both directories and files. The frontend refreshes
the tree after each tool completion or failure and again at run completion, so
new files and folders created by the Agent appear during the same run.

The implementation uses `GET /workspaces/select` with a native Windows folder
dialog on the local API process. This keeps the browser UI free of manual path
entry while preserving the original local Workspace security boundary. The
previous browser-upload implementation was removed.

The refresh path is optimized by scanning the local Workspace in a worker
thread, rendering the tree before the first file preview is loaded, and using
a short in-flight guarded polling interval during active runs. The frontend
renders a nested flat-row tree with collapsed folders and folder-first sorting
at each level.

Implementation is split into `frontend/src/AgentConsole.tsx` and the existing
stylesheet. TypeScript validation passes with `frontend/node_modules/.bin/tsc.cmd
--noEmit`. Vite build verification requires write access to Vite and TypeScript
cache directories under `frontend/node_modules`, which is environment-specific.

Why this matters:

The current Web UI already demonstrates SSE events. The next UI work should make the agent loop more legible, not merely prettier. The video should let evaluators immediately see model requests, tool calls, local execution, context changes, and final result.

Recommended improvements:

- Convert the event list into grouped iterations.
- Show each iteration as: model request -> model response -> tool calls -> tool results.
- Add a compact run summary: status, elapsed time, iterations, tool calls, current context size, final test status.
- Add filter tabs for `All`, `Model`, `Tools`, `Context`, and `Errors`.
- Add a side panel for selected event payloads instead of large inline JSON blocks.
- Add a workspace health strip showing workspace root, session id, provider mode, and active run id.
- Add a final answer panel with markdown-like formatting and monospace blocks.
- Add copy buttons for commands and event snippets.
- Improve empty, running, cancelled, failed, and completed states.
- Keep the UI as an operational console, not a landing page.

Visual direction:

- Use a restrained engineering-console style with good spacing, clear hierarchy, and readable typography.
- Avoid decorative hero sections, marketing copy, and overdesigned cards.
- Make tool activity the visual center of the page.
- Keep mobile usable, but prioritize desktop because the demo video will likely be recorded on desktop.

Acceptance criteria:

- A viewer can understand the agent loop without reading backend logs.
- Long tool outputs do not overwhelm the page.
- Errors, cancellations, context truncation, and final completion are visually distinct.
- `npm run build` succeeds.

Recommended effort:

- Medium-high value, low-medium risk. Good for polish after core upgrades.

## Priority 4: Observability and Replay

Status: implemented as a read-only JSONL trace and timeline viewer.

Why this matters:

A coding agent is easier to defend when every decision and tool result is observable. The current JSONL logger is a good start. A small replay or trace viewer would make the project feel much more mature.

Recommended improvements:

- Add a CLI flag such as `--event-log runs/demo.jsonl` to the demo command.
- Add a `coding_agent.trace` command or script that prints a readable summary from JSONL:
  - session id
  - user task
  - iterations
  - tool calls
  - command return codes
  - final answer
  - error if any
- Add event payload fields for context size and model usage when available.
- Add run duration and per-tool duration summaries.
- Add tests for the trace summary using a fixture JSONL file.

Implementation notes:

- `python -m coding_agent.trace <event-log>` prints the summary.
- Add `--timeline` for an event-by-event relative-time replay.
- Add `--json` for scripts and future UI integrations.
- Replay only parses recorded events; it never calls a model or local tool.

Acceptance criteria:

- A recorded run can be summarized after completion.
- The trace summary is useful for debugging and for explaining the architecture.
- Logging failures still never break agent execution.

Recommended effort:

- Medium value, low risk.

## Priority 5: Agent Planning and Reflection

Status: implemented as optional `plan` and `reflection` events around normal
assistant responses that include tool calls.

Why this is attractive but should be scoped carefully:

Planning makes the agent look smarter, but a poorly implemented planner can create noise. It should be a local protocol around the existing model loop, not a separate agent framework.

Recommended minimal version:

- Add optional plan events, not a separate planning subsystem.
- Encourage the model through the system prompt to state a short plan before tool use when the task is non-trivial.
- Parse no special free-form plan unless necessary.
- Store plan text as a normal assistant message or event payload.
- In the UI, display the latest plan above the iteration trace.

Possible later version:

- Add a lightweight `TaskState` object maintained locally:
  - goal
  - current phase
  - files inspected
  - commands run
  - tests passing or failing
- Feed this state into the context as a compact local memory.

Acceptance criteria:

- The plan improves readability without becoming required for every small task.
- The model can still proceed directly for simple tasks.
- Tests remain deterministic by using mock responses.

Implementation notes:

- The first tool-producing response in a turn is marked as `plan` when it has
  assistant text.
- Later tool-producing responses with text are marked as `reflection`.
- The text is still stored once as a normal assistant message in ContextManager.
- The Web UI shows the latest plan above the iteration trace.

Recommended effort:

- Medium value, medium risk. Good after context compression and observability.

## Priority 6: Broader Tool Set

Why this is lower priority:

More tools can make the agent more capable, but every new tool increases safety and testing burden. Add tools only when they strengthen the demo or clearly improve coding ability.

Recommended additions:

- `get_file_info`: size, modified time, whether text/binary, line count
- `list_directory_tree`: compact tree with ignore rules
- `run_tests`: a safer wrapper around common test commands with structured status
- `git_diff`: read-only diff of workspace changes, excluding `.git` metadata access
- `format_file`: optional formatter wrapper for known ecosystems

Avoid for this assignment:

- arbitrary package installation as a first-class tool
- network browsing tools
- direct Git mutation tools such as commit, reset, checkout, or push
- Docker or container orchestration unless the safety model is documented and tested

Acceptance criteria:

- Every new tool has a Pydantic schema, workspace validation, output limit, and tests.
- Tool descriptions are clear enough for model-native tool calling.
- The demo remains short and deterministic.

Recommended effort:

- Medium value, variable risk. Add only two or three tools at most before submission.

## Priority 7: Session Persistence

Why this is not urgent:

Persistence sounds impressive, but the assignment focuses on the agent loop and local execution. In-memory multi-turn conversation is easier to explain and already satisfies a useful capability. Persistence is only worth adding if the core and demo are already polished.

Recommended minimal version:

- Persist event JSONL and enough session metadata to inspect past runs.
- Do not restore live agent state at first.
- Add a read-only past-run viewer in the UI.

Avoid before deadline:

- SQLite-backed full conversation restore
- resuming partially completed tool calls
- multi-user authentication
- background job scheduling

Acceptance criteria:

- Past runs can be inspected.
- No credentials or large tool outputs are persisted by default.
- Persistence remains optional.

Recommended effort:

- Lower value before deadline, medium risk.

## Priority 8: Packaging and Developer Experience

Why this matters:

Small developer-experience improvements make the project easier for evaluators to run.

Recommended improvements:

- Add a `Makefile`, `justfile`, or simple scripts for common commands.
- Add `pytest --basetemp .pytest-run` to the documented Windows test command to avoid temp permission issues.
- Add `npm run build` to the verification checklist.
- Add a one-command backend startup example.
- Add a sample `.env.example` with placeholder values only.
- Add a short troubleshooting section for Windows event loop behavior and temp directory permissions.

Acceptance criteria:

- A fresh evaluator can run tests and the demo without guessing commands.
- Windows-specific pitfalls are documented.
- The commands do not expose secrets.

Recommended effort:

- Medium value, low risk.

## Recommended Execution Order

### Phase A: Must Finish Before Submission

1. Harden `README.txt` for the exact submission requirements.
2. Update `docs/DEMO_PLAN.md` with the final video script and exact commands.
3. Document the repo-local pytest command: `python -m pytest --basetemp .pytest-run-final`.
4. Rehearse the CLI demo three times with a real model.
5. Record the two-minute video.
6. Verify the zip contains only the required files and no secrets.

### Phase B: High-Impact Technical Upgrade

1. Add context stats and expose them in events.
2. Add deterministic context compaction for older turns.
3. Add tests for compaction, multi-turn memory, and tool-call grouping.
4. Show context budget and compaction events in the Web UI.

### Phase C: Capability Upgrade

1. Add a local patch-style edit tool.
2. Add rollback and stale-context protection.
3. Add tests for patch success and failure.
4. Update demo prompt only if the new edit tool makes the demo more reliable.

### Phase D: Presentation Upgrade

1. Group Web UI events by iteration.
2. Add run summary metrics.
3. Add event filters and selected-event details.
4. Polish final answer and error states.
5. Run `npm run build`.

### Phase E: Optional Maturity Features

1. Add JSONL trace summary command.
2. Add read-only git diff tool.
3. Add past-run viewer.
4. Add packaging scripts.

## Trade-Off Decisions

Recommended to do:

- context stats and deterministic compaction
- better Web UI event grouping
- patch-style local edit tool if time allows
- README and video hardening
- trace summary from JSONL

Recommended to postpone:

- full database persistence
- multi-agent collaboration
- plugin marketplace
- remote sandbox execution
- browser automation
- autonomous background tasks
- complex WebSocket protocol
- authentication and multi-user accounts

Reason:

Those postponed features either do not help the core assignment much, are hard to finish safely, or could make the project look less like a clean self-implemented coding agent and more like a thin wrapper around infrastructure.

## Final Target Narrative

The final project should be presented as:

```text
A self-implemented local coding agent. The model only proposes structured tool calls.
The repository implements the runtime loop, context management, tool registry,
tool execution, workspace safety, event stream, CLI, API, and UI. The demo shows
the agent reading a real project, running failing tests locally, editing code
locally, rerunning tests, and returning a final answer.
```

This narrative is strong because it maps directly to the assignment requirements and to what the code actually does.
