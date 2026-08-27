"""Best-effort JSONL persistence for observable Agent events."""

from __future__ import annotations

from pathlib import Path

from .event import AgentEvent


class JsonlEventLogger:
    """Append each event as one JSON object without affecting Agent execution."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, event: AgentEvent) -> bool:
        """Write one event and return False when logging fails."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(event.model_dump_json())
                stream.write("\n")
        except Exception:
            # Observability must never turn a successful Agent step into a failure.
            return False
        return True

    def __call__(self, event: AgentEvent) -> None:
        """Allow the logger to be used directly as an Agent event handler."""
        self.write(event)
