"""Status aggregation for the task-manager demo."""

from __future__ import annotations

from collections.abc import Iterable

from .models import VALID_STATUSES, Task


def summarize(tasks: Iterable[Task]) -> dict[str, object]:
    """Return total count, counts by status, and titles in input order."""
    items = list(tasks)
    counts = {status: 0 for status in VALID_STATUSES}
    for task in items:
        counts[task.status] += 1
    return {
        "total": counts["done"],
        "by_status": counts,
        "titles": [task.title for task in items],
    }
