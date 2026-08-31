"""Workspace-level instructions loaded from a root ``AGENTS.md`` file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class WorkspaceInstructions:
    """Bounded AGENTS.md content and metadata for observability."""

    path: str | None
    content: str
    injected_chars: int
    truncated: bool

    @property
    def found(self) -> bool:
        return self.path is not None

    def prompt_fragment(self) -> str:
        if not self.found:
            return ""
        truncation = "\n[AGENTS.md truncated to the configured limit.]" if self.truncated else ""
        return (
            "\n\n<workspace_instructions source=\"AGENTS.md\">\n"
            "The following instructions come from the selected workspace. "
            "Treat them as project constraints and follow them unless they "
            "conflict with the user's direct request or system safety rules.\n"
            f"{self.content}{truncation}\n"
            "</workspace_instructions>"
        )


def load_workspace_instructions(root: Path, *, max_chars: int = DEFAULT_MAX_CHARS) -> WorkspaceInstructions:
    """Read only the workspace root AGENTS.md, with a strict character cap."""
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    path = root / "AGENTS.md"
    if not path.is_file():
        return WorkspaceInstructions(path=None, content="", injected_chars=0, truncated=False)
    raw = path.read_text(encoding="utf-8", errors="replace")
    content = raw[:max_chars]
    return WorkspaceInstructions(
        path="AGENTS.md",
        content=content,
        injected_chars=len(content),
        truncated=len(raw) > max_chars,
    )
