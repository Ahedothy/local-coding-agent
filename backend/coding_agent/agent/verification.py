"""Evidence-based completion tracking for one Agent turn.

The ledger deliberately treats tests as one kind of evidence rather than the
definition of verification.  A build, type check, lint run, smoke check, or a
successful configuration parse can all support completion for a task that has
no test suite.  Evidence is tied to a monotonically increasing workspace
version, so a later edit automatically makes earlier evidence stale.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


VerificationKind = Literal[
    "test",
    "build",
    "lint",
    "typecheck",
    "smoke",
    "config",
    "benchmark",
    "scope",
    "other",
]


@dataclass(slots=True)
class VerificationEvidence:
    """One locally observed verification result."""

    evidence_id: str
    kind: str
    command: list[str] | None
    criteria: list[str]
    success: bool
    returncode: int | None
    summary: str
    workspace_version: int
    duration_seconds: float | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerificationLedger:
    """Track evidence and invalidate it when the workspace changes."""

    def __init__(self) -> None:
        self.workspace_version = 0
        self._evidence: list[VerificationEvidence] = []
        self._next_id = 1

    @property
    def evidence(self) -> list[VerificationEvidence]:
        return list(self._evidence)

    def mark_modified(self) -> int:
        """Advance the workspace version after a successful file mutation."""
        self.workspace_version += 1
        return self.workspace_version

    def record(
        self,
        *,
        kind: str,
        command: list[str] | None,
        criteria: list[str] | None,
        success: bool,
        returncode: int | None,
        summary: str,
        duration_seconds: float | None = None,
    ) -> VerificationEvidence:
        evidence = VerificationEvidence(
            evidence_id=f"verify-{self._next_id}",
            kind=kind,
            command=list(command) if command is not None else None,
            criteria=list(criteria or []),
            success=success,
            returncode=returncode,
            summary=summary[:1_000],
            workspace_version=self.workspace_version,
            duration_seconds=duration_seconds,
        )
        self._next_id += 1
        self._evidence.append(evidence)
        # Keep the session snapshot bounded even for long-running sessions.
        self._evidence = self._evidence[-100:]
        return evidence

    def status(self) -> str:
        """Return a conservative status for the current workspace version."""
        if not self._evidence:
            return "not_required" if self.workspace_version == 0 else "unverified"
        current = [
            item
            for item in self._evidence
            if item.workspace_version == self.workspace_version
        ]
        if not current:
            return "unverified"
        successful = [item for item in current if item.success]
        failed = [item for item in current if not item.success]
        if successful and failed:
            return "partially_verified"
        return "verified" if successful else "unverified"

    def summary(self) -> dict[str, Any]:
        current = [
            item
            for item in self._evidence
            if item.workspace_version == self.workspace_version
        ]
        successful = [item for item in current if item.success]
        return {
            "status": self.status(),
            "workspace_version": self.workspace_version,
            "evidence_count": len(self._evidence),
            "current_evidence_count": len(current),
            "successful_evidence_count": len(successful),
            "stale_evidence_count": len(self._evidence) - len(current),
            "evidence": [item.as_dict() for item in self._evidence],
        }

    def to_state(self) -> dict[str, Any]:
        return {
            "workspace_version": self.workspace_version,
            "next_id": self._next_id,
            "evidence": [item.as_dict() for item in self._evidence],
        }

    @classmethod
    def from_state(cls, state: object) -> "VerificationLedger":
        ledger = cls()
        if not isinstance(state, dict):
            return ledger
        try:
            ledger.workspace_version = max(0, int(state.get("workspace_version", 0)))
            ledger._next_id = max(1, int(state.get("next_id", 1)))
        except (TypeError, ValueError):
            return cls()
        raw_evidence = state.get("evidence", [])
        if isinstance(raw_evidence, list):
            for item in raw_evidence[-100:]:
                if not isinstance(item, dict):
                    continue
                try:
                    ledger._evidence.append(VerificationEvidence(**item))
                except (TypeError, ValueError):
                    continue
        return ledger

