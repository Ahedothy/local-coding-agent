"""Workspace boundary and path-safety utilities."""

from .workspace import Workspace
from .security import WorkspaceSecurityError

__all__ = ["Workspace", "WorkspaceSecurityError"]
