from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coding_agent.models import ToolCall
from coding_agent.tools import ToolContext, ToolExecutor, ToolRegistry
from coding_agent.tools import environment as environment_tools
from coding_agent.tools.environment import ENVIRONMENT_TOOLS
from coding_agent.workspace import Workspace


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(session_id="session-1", workspace=Workspace(tmp_path))


def execute(tmp_path: Path, arguments: dict) -> object:
    return asyncio.run(
        ToolExecutor(ToolRegistry(ENVIRONMENT_TOOLS)).execute(
            ToolCall(id="call-1", name="inspect_environment", arguments=arguments),
            make_context(tmp_path),
        )
    )


def test_environment_tool_is_registered() -> None:
    assert [tool.name for tool in ENVIRONMENT_TOOLS] == ["inspect_environment"]


def test_environment_tool_schema_is_available_to_the_registry() -> None:
    registry = ToolRegistry(ENVIRONMENT_TOOLS)

    assert registry.get("inspect_environment") is ENVIRONMENT_TOOLS[0]
    assert registry.model_schemas()[0]["name"] == "inspect_environment"


def test_inspect_environment_reports_project_markers_ports_and_redacts_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a marker\n", encoding="utf-8")
    for variable in environment_tools.SAFE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    monkeypatch.setattr(
        environment_tools,
        "PROBES",
        (environment_tools._Probe("runtime", "python", "python", ("--version",)),),
    )
    monkeypatch.setattr(
        environment_tools,
        "_probe",
        lambda probe: {
            "category": probe.category,
            "name": probe.name,
            "executable": "python.exe",
            "version": "Python 3.12.0",
            "available": True,
            "probe_succeeded": True,
        },
    )
    monkeypatch.setattr(
        environment_tools,
        "_check_port",
        lambda port: {"port": port, "available": port == 5173},
    )

    result = execute(tmp_path, {"ports": [4173, 5173]})

    assert result.success is True
    assert result.output["workspace"]["project_markers"] == [
        "package.json",
        "pyproject.toml",
    ]
    assert result.output["workspace"]["detected_ecosystems"] == ["Node.js", "Python"]
    assert result.output["probes"][0]["version"] == "Python 3.12.0"
    assert result.output["ports"] == [
        {"port": 4173, "available": False},
        {"port": 5173, "available": True},
    ]
    assert result.output["configured_environment_variables"] == ["OPENAI_API_KEY"]
    assert result.output["environment_values_redacted"] is True
    assert "super-secret-value" not in result.model_dump_json()


def test_inspect_environment_handles_empty_workspace_without_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_tools, "PROBES", ())

    result = execute(tmp_path, {"include_ports": False})

    assert result.success is True
    assert result.output["workspace"]["project_markers"] == []
    assert result.output["workspace"]["detected_ecosystems"] == []
    assert result.output["ports"] == []


def test_probe_runs_only_fixed_local_version_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(environment_tools.shutil, "which", lambda name: "C:/tools/python.exe")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"Python 3.12.1\n", stderr=b"")

    monkeypatch.setattr(environment_tools.subprocess, "run", fake_run)

    result = environment_tools._probe(
        environment_tools._Probe("runtime", "python", "python", ("--version",))
    )

    assert result["available"] is True
    assert result["probe_succeeded"] is True
    assert result["version"] == "Python 3.12.1"
    assert calls == [
        (
            ["C:/tools/python.exe", "--version"],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
                "check": False,
                "timeout": environment_tools.VERSION_TIMEOUT_SECONDS,
            },
        )
    ]


def test_inspect_environment_rejects_invalid_ports(tmp_path: Path) -> None:
    result = execute(tmp_path, {"ports": [0]})

    assert result.success is False
    assert "ports must be between 1 and 65535" in result.error


def test_inspect_environment_rejects_file_as_inspection_path(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")

    result = execute(tmp_path, {"path": "file.txt"})

    assert result.success is False
    assert "not a directory" in result.error
