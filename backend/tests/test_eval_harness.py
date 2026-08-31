from coding_agent.eval_harness import run_mock_evaluation

def test_mock_evaluation_completes_fixed_tasks():
    summary = run_mock_evaluation().as_dict()
    assert summary["tasks"] == 3
    assert summary["completed"] == 3
    assert summary["verified"] == 3
    assert summary["unsafe_writes"] == 0
