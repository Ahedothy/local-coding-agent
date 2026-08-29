lvyiyou-coding-agent
====================

A student-built coding agent whose important logic is implemented locally:

- explicit Agent Runtime loop
- provider-neutral model adapter
- Tool Registry and Tool Executor
- local filesystem and command tools
- workspace path and command safety boundary
- bounded ContextManager
- in-memory multi-turn conversation
- local SQLite run history, replay, and resumable conversations
- deterministic MockModelProvider tests

The project does not use LangChain, LangGraph, LlamaIndex, OpenAI Agents SDK,
Claude Agent SDK, AutoGen, CrewAI, or another Agent framework.

Requirements
------------

- Python 3.12 or newer
- pytest for the test suite
- an API key only when using the real provider

The current repository has been tested with Python 3.13 on Windows.

Setup
-----

From the repository root:

    python -m pip install -e ".[test]"

For real model calls, copy `.env.example` to `.env` and set the provider
configuration. `.env` is ignored by Git and must never be committed.

The supported variables are:

    OPENAI_API_KEY=your-key
    OPENAI_BASE_URL=https://api.openai.com/v1
    OPENAI_MODEL=your-model-name

The provider is OpenAI-compatible. The model must support tool calling for
coding tasks that need local tools.

Run Tests
---------

From the repository root:

    python -m pytest

The automated tests use MockModelProvider and do not require a network or API
key. One Windows symlink test may be skipped when the current account cannot
create symbolic links.

One-Shot Mode
-------------

Run the CLI from the `backend` directory. The workspace can be any existing
directory; it does not have to be inside this repository.

    cd backend
    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\workspace" `
      --provider real `
      "Fix the failing tests. Read the code, run the tests, make the minimal changes, and run the tests again."

For a local smoke test without an API key:

    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\workspace" `
      --provider mock `
      "Inspect the project"

Direct filesystem tools are restricted to the configured workspace. Commands
are started locally with a validated workspace working directory, argument-array
invocation, `shell=False`, a timeout, bounded output, and an approval gate for
side effects.

Event Logs and Trace Replay
---------------------------

Record a run as JSONL by adding `--event-log`:

    cd backend
    python -m coding_agent.cli `
      --workspace "C:\path\to\workspace" `
      --provider real `
      --event-log "..\event_logs\demo.jsonl" `
      "Inspect the project, fix the failing tests, and run them again."

After the run, print a readable summary and chronological replay:

    python -m coding_agent.trace "..\event_logs\demo.jsonl" --timeline

Use `--json` instead of the default text format when another script needs to
consume the summary. The trace command is read-only: it parses recorded events,
but never calls the model, executes tools, or restores a live conversation.

For non-trivial tasks, the model may include a short plan before its first tool
call and a short reflection after later tool results. These are optional event
annotations around the existing Agent loop, and simple tasks can still go
directly to an answer.

Run History and Replay
----------------------

The Web API records run metadata, every event, the latest session state, and an
exact bounded snapshot for each run in a local SQLite database. The default database is located in the
backend data directory:

    backend\history.db

Set `CODING_AGENT_HISTORY_DIR` to choose another local database path (or a
directory in which `history.db` should be created). The Web UI loads this
history into Recent tasks, grouped by conversation session. A new session is
shown immediately as `New conversation`; after its first message, a short
stable title is generated from that message and is never changed by later
runs. Selecting a task
replays all recorded turns, tool activity, approvals, errors, final answers,
and changes for that session. Replay is read-only: it never calls the model,
executes tools, restores a pending approval, or changes the current workspace.
The `Continue` action then restores the historical workspace and the latest
bounded ContextManager snapshot for that conversation, and creates a new user
turn in the same conversation. Tool output is bounded but is not
automatically secret-redacted, so keep a custom history database local and do
not use it for shared or sensitive logs.

Interactive Multi-Turn Mode
----------------------------

Interactive mode creates one Agent and reuses its Session and ContextManager
for every user turn in the process:

    cd backend
    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\workspace" `
      --provider real `
      --interactive

Then enter messages at the `>` prompt:

    你好，请简单介绍一下你能做什么。
    请读取这个项目的结构，告诉我它的功能。
    现在运行测试并修复失败的测试，尽量只做最小修改。

Enter `/exit` or `/quit` to leave the conversation. Blank input is ignored.
An optional initial task can be supplied after `--interactive`; it is handled
as the first turn before the CLI begins reading input.

    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\workspace" `
      --provider real `
      --interactive `
      "先分析这个项目"

Manual Multi-Turn Verification
------------------------------

1. Start interactive mode with a small workspace.
2. Ask the agent to inspect the project.
3. Ask a follow-up question that depends on the previous answer or tool result.
4. Confirm that the second turn produces a new `user_message`, `model_request`,
   and `agent_finished` event while using the same running process.
5. Ask it to make a small code change and run the tests.
6. Exit with `/exit`.

The conversation is held in memory only. Closing the CLI process discards the
conversation. Session persistence and conversation restore are not implemented.
Use the real provider for manual multi-turn
verification; the CLI mock provider is a one-shot smoke-test provider, while
multi-turn mock behavior is covered by the automated Agent Core tests.

Web UI
------

The optional web interface uses the existing Agent Core through FastAPI and
SSE. Start the backend from one PowerShell window:

    cd backend
    python -m uvicorn coding_agent.api:app --port 8000

Start the React development server from a second PowerShell window:

    cd frontend
    npm install
    npm run dev

Open the URL printed by Vite, usually `http://127.0.0.1:5173/`. Enter an
existing local workspace path, create a session, and submit a task. The page
shows a streaming conversation: the user task appears first, model and local
tool activity is grouped into a collapsible Thinking Process, and the final
answer is progressively revealed as the run completes. The workspace,
conversation, and file preview are independent panes; desktop users can resize
or collapse the side panes, while the message composer remains docked at the
bottom of the center pane. A new session also offers starter actions for
exploring a codebase, building a feature, reviewing code, or fixing a bug;
clicking one fills an editable task prompt without starting a run automatically.

On Windows, do not start the backend with `--reload`. The reload subprocess can
select an asyncio event loop that does not support `create_subprocess_exec`,
which causes `execute_command` to fail with `NotImplementedError`. Restart the
backend manually after changing Python code.

The web session supports multiple user turns while the browser page and backend
process remain alive. Refreshing the page or changing the workspace creates a
new in-memory session, while completed run history remains available through
the local SQLite history store. A historical conversation can be continued
explicitly from Recent tasks.

Local Inspection Tools
----------------------

The Agent also has four bounded, read-only inspection tools:

- `get_file_info` reports file or directory metadata, detected file type and
  MIME type, text/binary status, best-effort encoding and confidence, and line
  count when the file is within the inspection limit. It recognizes BOMs,
  common legacy text encodings, and common binary file signatures while making
  heuristic results explicit.
- `list_directory_tree` shows a compact directory-first tree with depth,
  entry, and generated-directory ignore rules.
- `git_diff` shows local Git status and a bounded unified diff without exposing
  `.git` metadata or mutating the repository.
- `inspect_environment` reports local platform details, fixed runtime/compiler/
  package-manager probes, project marker files, redacted configuration presence,
  and candidate localhost port availability. It does not accept arbitrary
  commands or modify the workspace.

These tools operate on the selected local Workspace. File paths and command
working directories are checked by the Workspace security boundary. The
existing `execute_command` remains the path for running tests, so test commands
keep one consistent timeout, cwd, shell, and exit-status policy.
Formatter-specific execution, package installation, network browsing, and Git
mutation are intentionally outside the current tool set.

Local Process Management
------------------------

For a development server or interactive local program that must remain alive
across multiple Agent calls, use `manage_process`. It supports `start`, `list`,
`status`, `read`, `write`, and `stop`. The start command is still an argument
array, not a shell string; output is drained locally into bounded buffers, and
the returned process handle is used for later operations. Starting, writing,
and stopping require local approval. The tool does not install packages, create
detached system services, or restore processes after the Agent session ends.

Demo Repository
---------------

The deterministic demo is in `demo_task_manager`. It is a small multi-file task
management library with intentional defects in search, pagination, and report
aggregation. Reset it to the failing state before each real-model rehearsal,
then run:

    cd backend
    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\lvyiyou-coding-agent\\demo_task_manager" `
      --provider real `
      "Please fix the failing tests in this small Python project. Inspect the tests and implementation first, make the minimal multi-file change, and run the complete test suite again."

The expected proof is visible in the event stream:

    list_files -> read_file (tests and implementation) -> execute_command (failure)
    -> apply_patch (two files) -> execute_command (success) -> final answer

The model requests tools, but all file reads, edits, and test commands happen
in the local ToolExecutor inside the configured workspace.

Project Documentation
---------------------

- `docs/ARCHITECTURE.md`: system architecture and module boundaries
- `docs/IMPLEMENTATION_PLAN.md`: milestone order and completion criteria
- `docs/IMPLEMENTATION_RULES.md`: constraints for future implementation work
- `docs/TEST_PLAN.md`: test strategy and required scenarios
- `docs/DEMO_PLAN.md`: bug-fix demo and video plan
- `docs/MULTI_TURN_PLAN.md`: multi-turn design and implementation scope
