"""Content fingerprints used for optimistic file concurrency checks."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_sha256(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def ensure_expected_sha256(path: Path, expected: str | None, relative_path: str) -> None:
    if expected is None:
        return
    current = current_sha256(path)
    if current != expected:
        raise ValueError(
            "stale file: "
            f"{relative_path} content fingerprint mismatch "
            f"(expected sha256={expected}, current sha256={current or 'missing'}); "
            "read the file again before editing"
        )
