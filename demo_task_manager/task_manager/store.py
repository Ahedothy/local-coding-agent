"""In-memory persistence for the task-manager demo."""

from __future__ import annotations

from collections.abc import Iterable

from .models import VALID_STATUSES, Task


class TaskStore:
    """Keep tasks in insertion order while enforcing basic invariants."""

    def __init__(self, tasks: Iterable[Task] = ()) -> None:
        self._tasks: dict[int, Task] = {}
        for task in tasks:
            if task.task_id in self._tasks:
                raise ValueError(f"duplicate task id: {task.task_id}")
            self._tasks[task.task_id] = task
        self._next_id = max(self._tasks, default=0) + 1

    def add(self, title: str) -> Task:
        task = Task(task_id=self._next_id, title=title)
        self._tasks[task.task_id] = task
        self._next_id += 1
        return task

    def all(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())

    def update_status(self, task_id: int, status: str) -> Task:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        try:
            current = self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"task not found: {task_id}") from exc
        updated = Task(task_id=current.task_id, title=current.title, status=status)
        self._tasks[task_id] = updated
        return updated
