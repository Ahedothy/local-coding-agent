"""Provider-neutral contracts for locally executed tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_agent.workspace import Workspace


@dataclass(slots=True)
class ToolContext:
    """Runtime dependencies supplied to a tool by ToolExecutor."""

    session_id: str
    workspace: Workspace
    config: Any = None
    cancellation_token: Any = None


class ToolResult(BaseModel):
    """Standardized result that can be appended to model context."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    success: bool
    output: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_error_state(self) -> "ToolResult":
        if self.success and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        if not self.success and not self.error:
            raise ValueError("failed tool results require an error")
        return self


class Tool(ABC):
    """Base contract implemented by each concrete local tool."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters_model: ClassVar[type[BaseModel]]

    @classmethod
    def model_schema(cls) -> dict[str, Any]:
        """Return the provider-neutral, model-visible tool schema."""
        return {
            "name": cls.name,
            "description": cls.description,
            "parameters": cls.parameters_model.model_json_schema(),
        }

    @abstractmethod
    async def execute(
        self,
        context: ToolContext,
        arguments: BaseModel,
    ) -> ToolResult:
        """Execute validated arguments locally."""

