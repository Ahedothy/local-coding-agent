"""Domain objects for the task-manager demo."""

from __future__ import annotations

from dataclasses import dataclass


VALID_STATUSES = ("todo", "doing", "done")


@dataclass(frozen=True, slots=True)
class Task:
    """One task with a stable identifier and a small status vocabulary."""

    task_id: int
    title: str
    status: str = "todo"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("task title must not be empty")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")
