from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.config import DotEnvError, load_dotenv


def test_load_dotenv_sets_values_and_preserves_existing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "OPENAI_API_KEY='from-file'\n"
        "OPENAI_MODEL=demo-model\n"
        "export OPENAI_BASE_URL=https://example.test/v1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://explicit.test/v1")

    loaded = load_dotenv(dotenv_path)

    assert loaded == dotenv_path
    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert os.environ["OPENAI_MODEL"] == "demo-model"
    assert os.environ["OPENAI_BASE_URL"] == "https://explicit.test/v1"


def test_load_dotenv_missing_file_is_harmless(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / ".env") is None


def test_load_dotenv_rejects_malformed_entry(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("not-an-assignment\n", encoding="utf-8")

    with pytest.raises(DotEnvError, match="line 1"):
        load_dotenv(dotenv_path)
