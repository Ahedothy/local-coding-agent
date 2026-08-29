"""Small task-management library used by the coding-agent demo."""

from .models import Task
from .service import TaskService
from .store import TaskStore

__all__ = ["Task", "TaskService", "TaskStore"]
