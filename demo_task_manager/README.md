# Task Manager Demo

This is the deterministic multi-file repository used to demonstrate the local
coding agent. It is a small, dependency-light task management library with an
in-memory store, a service layer, and a status report.

Use the test suite as the executable specification: inspect the project, run
the tests, locate the causes of any failures, make minimal implementation
changes, and run the full suite again. The repository intentionally leaves the
diagnosis to the agent rather than documenting the expected fixes here.

Run the tests from this directory:

```text
python -m pytest -q
```

The initial test run is expected to provide the first diagnostic signal. A
successful run should finish with all tests passing while preserving the
existing public API.
