"""Small, dependency-free configuration helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path


class DotEnvError(ValueError):
    """Raised when a non-empty line in ``.env`` is malformed."""


_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load simple ``KEY=VALUE`` entries without overwriting the environment.

    When no path is supplied, the current directory is checked first, then
    the repository root relative to this package. Missing files are harmless.
    This intentionally supports only the small configuration format needed by
    the project; it is not intended to be a full dotenv implementation.
    """
    candidates = [path] if path is not None else [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    dotenv_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if dotenv_path is None:
        return None

    for line_number, raw_line in enumerate(
        dotenv_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DotEnvError(f"invalid .env entry on line {line_number}")

        key, value = (part.strip() for part in line.split("=", 1))
        if not _KEY_PATTERN.fullmatch(key):
            raise DotEnvError(f"invalid .env key on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return dotenv_path
