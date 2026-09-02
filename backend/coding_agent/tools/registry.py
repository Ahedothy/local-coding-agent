"""Registry for discovering tools without executing them."""

from __future__ import annotations

from collections.abc import Iterable

from .base import Tool


class ToolRegistry:
    """Store tools by name and expose their model-visible schemas."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        if tools is not None:
            for tool in tools:
                self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register one tool, rejecting invalid or duplicate names."""
        if not isinstance(tool, Tool):
            raise TypeError("tool must be an instance of Tool")
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool is already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Return a registered tool or None when the name is unknown."""
        return self._tools.get(name)

    def model_schemas(self) -> list[dict]:
        """Return schemas generated from each tool's Pydantic argument model."""
        return [tool.model_schema() for tool in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        """Return registered tool names for diagnostics and prompts."""
        return tuple(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
