"""Application operations for the task-manager demo."""

from __future__ import annotations

from .models import Task
from .store import TaskStore


class TaskService:
    """Expose search, pagination, and status updates over a TaskStore."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def create(self, title: str) -> Task:
        return self.store.add(title)

    def list_tasks(
        self,
        *,
        query: str = "",
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Task]:
        if offset < 0:
            raise ValueError("offset must not be negative")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        tasks = list(self.store.all())
        if query.strip():
            needle = query.strip()
            tasks = [task for task in tasks if needle in task.title]
        if status is not None:
            tasks = [task for task in tasks if task.status == status]

        return tasks[offset + 1 : offset + 1 + limit]

    def set_status(self, task_id: int, status: str) -> Task:
        return self.store.update_status(task_id, status)
