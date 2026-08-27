"""Workspace abstraction shared by all future local tools."""

from __future__ import annotations

from pathlib import Path

from .security import WorkspaceSecurityError, validate_workspace_path


class Workspace:
    """Resolve and validate paths relative to one configured workspace root.

    This class is a policy boundary, not an operating-system sandbox. Callers
    must use its resolution methods before reading, writing, or choosing a
    subprocess working directory.
    """

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser().resolve()
        if not candidate.exists():
            raise WorkspaceSecurityError(f"Workspace does not exist: {candidate}")
        if not candidate.is_dir():
            raise WorkspaceSecurityError(f"Workspace is not a directory: {candidate}")
        if candidate.name.casefold() == ".git":
            raise WorkspaceSecurityError("The workspace root cannot be .git")
        self.root = candidate

    def resolve_path(self, path: str | Path) -> Path:
        """Resolve an existing or future path and enforce the workspace policy."""
        candidate = Path(path)
        resolved = (self.root / candidate).resolve()
        validate_workspace_path(resolved, self.root)
        return resolved

    def resolve_existing_path(self, path: str | Path) -> Path:
        """Resolve a path that must already exist inside the workspace."""
        resolved = self.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        return resolved

    def resolve_cwd(self, cwd: str | Path | None = None) -> Path:
        """Resolve a subprocess cwd, defaulting to the workspace root."""
        resolved = self.resolve_existing_path(self.root if cwd is None else cwd)
        if not resolved.is_dir():
            raise WorkspaceSecurityError(f"Command cwd is not a directory: {resolved}")
        return resolved

    def ensure_readable_file(self, path: str | Path) -> Path:
        """Resolve a path that must be a regular, non-symlinked file target."""
        resolved = self.resolve_existing_path(path)
        if not resolved.is_file():
            raise WorkspaceSecurityError(f"Path is not a file: {resolved}")
        return resolved

    def ensure_writable_file(self, path: str | Path) -> Path:
        """Resolve a file target while allowing it to be created later."""
        resolved = self.resolve_path(path)
        if resolved.exists() and not resolved.is_file():
            raise WorkspaceSecurityError(f"Path is not a file: {resolved}")
        return resolved

