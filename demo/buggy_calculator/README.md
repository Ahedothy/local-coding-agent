# Buggy Calculator Demo

This is the small repository used to demonstrate the local coding agent.
The implementation intentionally contains two bugs. The agent should inspect
the files, run the tests, make the minimal local edit, and run the tests again.

Run the tests from this directory:

```text
python -m pytest -q
```

The expected initial state is two failing tests. After the agent fixes
`calculator.py`, all tests should pass.
