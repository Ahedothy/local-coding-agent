# Task Manager Demo

This is the deterministic multi-file repository used to demonstrate the local
coding agent. It is a small, dependency-light task management library with an
in-memory store, a service layer, and a status report. The initial repository
contains intentional defects in search/pagination and report aggregation.

The agent should inspect the tests and implementation, run the failing test
suite, make the minimal changes across the affected files, and run the full
suite again.

Run the tests from this directory:

```text
python -m pytest -q
```

The expected initial state has failing tests in `test_task_manager.py`. A
successful run should finish with all tests passing and a diff touching
`task_manager/service.py` and `task_manager/report.py`.
