# Implementation Rules

## Purpose

This project is a student-built coding agent. The main goal is to demonstrate a clear, self-implemented Agent Runtime, Tool System, Context Manager, Workspace boundary, Model Adapter, Event System, and testable execution loop.

Correct architecture is more important than adding many features.

## Hard Rules

1. Do not introduce any Agent framework or Agent SDK.
   - Forbidden examples: LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, CrewAI, or any library that implements the agent loop or tool orchestration for us.
   - Official model API clients are allowed.

2. Do not delegate core logic to hosted tools.
   - Do not use Code Interpreter.
   - Do not use hosted file tools.
   - Do not use a provider Files API as the agent workspace.
   - File reading, file writing, command execution, and tool execution must happen locally.

3. Do not change the architecture without reporting first.
   - Follow `docs/ARCHITECTURE.md`.
   - If the architecture seems wrong, stop and explain the issue before refactoring.

4. Implement one milestone at a time.
   - Follow `docs/IMPLEMENTATION_PLAN.md`.
   - Do not mix unrelated milestones in one change.

5. Inspect existing code before modifying it.
   - Read the relevant module and tests first.
   - Preserve module boundaries.

6. Do not rewrite the whole project to solve a local problem.
   - Prefer small, focused changes.
   - Keep public interfaces stable unless the milestone explicitly changes them.

7. Keep Agent Core independent from UI.
   - React must not contain Agent loop logic.
   - FastAPI must not contain tool orchestration logic.
   - CLI and Web must both call the same Agent Core.

8. Every new feature must have tests.
   - Tool behavior requires unit tests.
   - Workspace security behavior requires tests.
   - Agent loop behavior must be tested with a mock model.

9. Do not hardcode API keys or secrets.
   - Use environment variables.
   - Keep `.env` out of Git.
   - Maintain `.env.example`.

10. Do not weaken the workspace safety boundary.
    - All file paths must go through `Workspace`.
    - Commands must run inside the workspace.
    - Path traversal and symlink escape must be rejected.

11. Run tests after each milestone.
    - At minimum run the tests related to the changed modules.
    - Before a commit, run the full backend test suite.

12. Do not add dependencies without clear value.
    - Prefer Python standard library when reasonable.
    - Added dependencies must be justified by the milestone.

13. Do not add complexity just to look advanced.
    - Avoid premature SQLite, RAG, multi-agent systems, plugin systems, complex approval systems, or elaborate UI.
    - Stable Agent loop and Tool System come first.

14. Preserve explainability.
    - The student must be able to explain every major component in an interview.
    - Prefer explicit, readable code over clever abstractions.

15. Keep model-provider code isolated.
    - Agent Runtime must depend on `ModelProvider`, not directly on OpenAI SDK classes.
    - Provider-specific parsing belongs in `models/`.

16. Keep tool execution isolated.
    - ToolRegistry discovers tools.
    - ToolExecutor validates and executes tools.
    - Individual tools implement only their own behavior.

17. Treat real LLM calls as integration behavior.
    - Unit tests must use `MockModelProvider`.
    - CI-style tests must not require a real API key.

18. Make the demo stable.
    - The demo repository should be small.
    - The task should require reading, testing, editing, and retesting.
    - Do not rely on unpredictable long model behavior for the video.

19. Keep patch editing local and structured.
    - `replace_in_file` handles one simple exact replacement.
    - `apply_patch` may accept standard unified diff text, but must validate
      every path through `Workspace`, reject unsafe or binary edits, and keep
      the local rollback behavior explainable and tested.

20. Keep planning lightweight and optional.
    - Plan and reflection events are annotations around normal model responses.
    - Do not introduce a separate planner, second Agent loop, or extra planning
      model call.
    - Simple tasks must still work without plan text.

21. Do not make Web UI the first deliverable.
    - CLI-first is the required MVP path.
    - FastAPI, SSE, and React are Phase 2 after Agent Core is stable.

## Definition of Done for Each Milestone

A milestone is done only when:

- the intended modules are implemented
- relevant tests are added
- relevant tests pass
- no forbidden dependency is introduced
- no API key or secret is committed
- architecture boundaries are preserved
- the student can explain what changed and why

## Required Implementation Attitude

When in doubt, choose the simpler design that keeps the Agent loop explicit and testable.

Do not optimize for looking impressive. Optimize for correctness, clarity, local execution, and interview explainability.
