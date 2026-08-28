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

The Agent can only access files through the configured workspace. Commands are
executed locally with a workspace working directory, `shell=False`, a timeout,
and bounded output.

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
shows model requests, local tool calls, tool results, and the final answer.

On Windows, do not start the backend with `--reload`. The reload subprocess can
select an asyncio event loop that does not support `create_subprocess_exec`,
which causes `execute_command` to fail with `NotImplementedError`. Restart the
backend manually after changing Python code.

The web session supports multiple user turns while the browser page and backend
process remain alive. Refreshing the page or changing the workspace creates a
new in-memory session.

Demo Repository
---------------

The deterministic demo is in `demo/buggy_calculator`. It intentionally contains
two bugs and tests that expose them. Reset it to the failing state before each
real-model rehearsal, then run:

    cd backend
    python -m coding_agent.cli `
      --workspace "C:\\path\\to\\lvyiyou-coding-agent\\demo\\buggy_calculator" `
      --provider real `
      "Please fix the failing tests in this small Python project. Read the code, run the tests, make the minimal code change, and run the tests again."

The expected proof is visible in the event stream:

    list_files -> read_file -> execute_command (failure)
    -> replace_in_file -> execute_command (success) -> final answer

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
