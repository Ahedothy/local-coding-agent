"""Read-only local environment detection for the coding agent."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from .base import Tool, ToolContext, ToolResult
from .filesystem import IGNORED_DIRECTORY_NAMES, _relative_path


MAX_PORTS = 20
PORT_CHECK_TIMEOUT_SECONDS = 0.2
VERSION_TIMEOUT_SECONDS = 3.0
MAX_VERSION_CHARS = 160

DEFAULT_PORTS = (3000, 4173, 5173, 8000, 8080)


class InspectEnvironmentArguments(BaseModel):
    """Bounded options for a local environment snapshot."""

    path: str = Field(
        default=".",
        description="Existing workspace directory whose project markers should be inspected.",
    )
    include_ports: bool = Field(
        default=True,
        description="Check whether the common local development ports can be bound.",
    )
    ports: list[int] = Field(
        default_factory=lambda: list(DEFAULT_PORTS),
        max_length=MAX_PORTS,
        description="Candidate localhost TCP ports to check when include_ports is true.",
    )

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, value: list[int]) -> list[int]:
        if any(port < 1 or port > 65535 for port in value):
            raise ValueError("ports must be between 1 and 65535")
        if len(set(value)) != len(value):
            raise ValueError("ports must not contain duplicates")
        return value


@dataclass(frozen=True)
class _Probe:
    category: str
    name: str
    executable: str
    arguments: tuple[str, ...]


# These are deliberately fixed probes. The model cannot turn this tool into an
# arbitrary command runner or use it to install packages.
PROBES = (
    _Probe("runtime", "python", "python", ("--version",)),
    _Probe("runtime", "node", "node", ("--version",)),
    _Probe("runtime", "java", "java", ("-version",)),
    _Probe("runtime", "go", "go", ("version",)),
    _Probe("runtime", "rustc", "rustc", ("--version",)),
    _Probe("runtime", "dotnet", "dotnet", ("--version",)),
    _Probe("compiler", "gcc", "gcc", ("--version",)),
    _Probe("compiler", "clang", "clang", ("--version",)),
    _Probe("compiler", "javac", "javac", ("-version",)),
    _Probe("build_tool", "cmake", "cmake", ("--version",)),
    _Probe("build_tool", "make", "make", ("--version",)),
    _Probe("package_manager", "pip", "pip", ("--version",)),
    _Probe("package_manager", "uv", "uv", ("--version",)),
    _Probe("package_manager", "poetry", "poetry", ("--version",)),
    _Probe("package_manager", "npm", "npm", ("--version",)),
    _Probe("package_manager", "pnpm", "pnpm", ("--version",)),
    _Probe("package_manager", "yarn", "yarn", ("--version",)),
    _Probe("package_manager", "cargo", "cargo", ("--version",)),
)

PROJECT_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("Pipfile", "Python"),
    ("poetry.lock", "Python"),
    ("package.json", "Node.js"),
    ("package-lock.json", "Node.js"),
    ("pnpm-lock.yaml", "Node.js"),
    ("yarn.lock", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("Cargo.lock", "Rust"),
    ("go.mod", "Go"),
    ("go.sum", "Go"),
    ("pom.xml", "Java"),
    ("build.gradle", "Java"),
    ("CMakeLists.txt", "C/C++"),
    ("Makefile", "C/C++"),
    ("CMakePresets.json", "C/C++"),
    ("*.sln", ".NET"),
    ("*.csproj", ".NET"),
)

SAFE_ENVIRONMENT_VARIABLES = (
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
)


def _success(tool: Tool, output: Any) -> ToolResult:
    return ToolResult(
        tool_call_id="pending",
        tool_name=tool.name,
        success=True,
        output=output,
    )


def _short_text(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:MAX_VERSION_CHARS]
    return ""


def _python_executable() -> str | None:
    """Use the current interpreter when PATH does not expose ``python``."""
    executable = shutil.which("python")
    if executable:
        return executable
    if Path(sys.executable).is_file():
        return sys.executable
    return None


def _probe(probe: _Probe) -> dict[str, Any]:
    executable = _python_executable() if probe.name == "python" else shutil.which(probe.executable)
    base = {
        "category": probe.category,
        "name": probe.name,
        "executable": executable,
        "version": None,
        "available": executable is not None,
        "probe_succeeded": False,
    }
    if executable is None:
        return {**base, "error": "executable not found on PATH"}

    try:
        result = subprocess.run(
            [executable, *probe.arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return {**base, "available": False, "error": f"could not start probe: {exc}"}
    except subprocess.TimeoutExpired:
        return {**base, "error": "version probe timed out"}

    version = _short_text(result.stdout) or _short_text(result.stderr)
    output = {
        **base,
        "version": version or None,
        "returncode": result.returncode,
        "probe_succeeded": result.returncode == 0,
    }
    if result.returncode != 0:
        output["error"] = version or f"version probe exited with code {result.returncode}"
    return output


def _check_port(port: int) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PORT_CHECK_TIMEOUT_SECONDS)
    try:
        sock.bind(("127.0.0.1", port))
        return {"port": port, "available": True}
    except OSError as exc:
        return {"port": port, "available": False, "reason": str(exc)[:MAX_VERSION_CHARS]}
    finally:
        sock.close()


def _project_markers(workspace_root: Path, directory: Path) -> tuple[list[str], list[str]]:
    """Inspect only the selected directory; recursive discovery is unnecessary here."""
    files: list[str] = []
    ecosystems: set[str] = set()
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return files, []

    for child in children:
        if child.is_symlink() or not child.is_file():
            continue
        matched_ecosystems = {
            ecosystem
            for marker, ecosystem in PROJECT_MARKERS
            if child.name == marker or (marker.startswith("*") and child.match(marker))
        }
        if matched_ecosystems:
            files.append(_relative_path(workspace_root, child))
            ecosystems.update(matched_ecosystems)
    return files, sorted(ecosystems, key=str.casefold)


class InspectEnvironmentTool(Tool):
    name: ClassVar[str] = "inspect_environment"
    description: ClassVar[str] = (
        "Inspect the local platform, fixed runtime and package-manager versions, "
        "workspace project markers, safe configuration presence, and candidate "
        "localhost ports without modifying files or running arbitrary commands."
    )
    parameters_model: ClassVar[type[BaseModel]] = InspectEnvironmentArguments

    async def execute(
        self,
        context: ToolContext,
        arguments: InspectEnvironmentArguments,
    ) -> ToolResult:
        directory = context.workspace.resolve_existing_path(arguments.path)
        if not directory.is_dir():
            raise ValueError(f"environment inspection path is not a directory: {arguments.path}")

        marker_files, ecosystems = _project_markers(context.workspace.root, directory)
        probe_results = await asyncio.gather(
            *(asyncio.to_thread(_probe, probe) for probe in PROBES)
        )
        ports = []
        if arguments.include_ports:
            ports = await asyncio.gather(
                *(asyncio.to_thread(_check_port, port) for port in arguments.ports)
            )

        configured_variables = [
            name for name in SAFE_ENVIRONMENT_VARIABLES if os.getenv(name)
        ]
        return _success(
            self,
            {
                "path": _relative_path(context.workspace.root, directory),
                "platform": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                    "python_version": platform.python_version(),
                    "shell": Path(
                        os.environ.get("COMSPEC") or os.environ.get("SHELL") or ""
                    ).name
                    or None,
                },
                "workspace": {
                    "root_name": context.workspace.root.name,
                    "inspected_path": _relative_path(context.workspace.root, directory),
                    "project_markers": marker_files,
                    "detected_ecosystems": ecosystems,
                },
                "probes": list(probe_results),
                "configured_environment_variables": configured_variables,
                "environment_values_redacted": True,
                "ports": list(ports),
                "port_check_scope": "127.0.0.1",
            },
        )


ENVIRONMENT_TOOLS: tuple[Tool, ...] = (InspectEnvironmentTool(),)
