from __future__ import annotations

import pytest

from task_manager import Task, TaskService, TaskStore
from task_manager.report import summarize


def make_service() -> TaskService:
    store = TaskStore(
        [
            Task(1, "Fix Login", "doing"),
            Task(2, "Write documentation", "todo"),
            Task(3, "Fix logout", "done"),
            Task(4, "Release checklist", "todo"),
        ]
    )
    return TaskService(store)


def test_create_assigns_next_id() -> None:
    service = make_service()
    created = service.create("Review pull request")
    assert created.task_id == 5
    assert created.status == "todo"


def test_search_is_case_insensitive() -> None:
    service = make_service()
    found = service.list_tasks(query="fix")
    assert [task.task_id for task in found] == [1, 3]


def test_pagination_uses_zero_based_offset() -> None:
    service = make_service()
    page = service.list_tasks(offset=1, limit=2)
    assert [task.task_id for task in page] == [2, 3]


def test_status_filter_and_update() -> None:
    service = make_service()
    service.set_status(2, "doing")
    found = service.list_tasks(status="doing")
    assert [task.task_id for task in found] == [1, 2]


def test_invalid_status_and_missing_task_are_rejected() -> None:
    service = make_service()
    with pytest.raises(ValueError, match="invalid task status"):
        service.set_status(1, "blocked")
    with pytest.raises(KeyError, match="task not found"):
        service.set_status(99, "done")


def test_report_counts_all_tasks_and_preserves_titles() -> None:
    tasks = list(make_service().store.all())
    assert summarize(tasks) == {
        "total": 4,
        "by_status": {"todo": 2, "doing": 1, "done": 1},
        "titles": [
            "Fix Login",
            "Write documentation",
            "Fix logout",
            "Release checklist",
        ],
    }
