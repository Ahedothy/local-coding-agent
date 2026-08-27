"""Small, explicit security helpers for the local workspace boundary."""

from __future__ import annotations

from pathlib import Path


class WorkspaceSecurityError(ValueError):
    """Raised when a path or working directory violates workspace policy."""


def is_within_workspace(path: Path, root: Path) -> bool:
    """Return whether *path* is equal to or below *root*."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def contains_git_component(path: Path) -> bool:
    """Return whether a path addresses the repository metadata directory."""
    return any(part.casefold() == ".git" for part in path.parts)


def validate_workspace_path(path: Path, root: Path) -> None:
    """Apply the common workspace and repository-metadata restrictions."""
    if not is_within_workspace(path, root):
        raise WorkspaceSecurityError(
            f"Path is outside the workspace: {path}"
        )
    if contains_git_component(path):
        raise WorkspaceSecurityError("Access to .git is not allowed")
