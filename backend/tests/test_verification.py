from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from coding_agent.agent import Agent, Session, VerificationLedger
from coding_agent.context import ContextManager
from coding_agent.events import AgentEventType
from coding_agent.models import MockModelProvider, ModelResponse, ToolCall
from coding_agent.tools import Tool, ToolContext, ToolExecutor, ToolRegistry, ToolResult
from coding_agent.tools.command import COMMAND_TOOLS
from coding_agent.workspace import Workspace


def test_ledger_accepts_non_test_evidence_and_invalidates_it_after_edit() -> None:
    ledger = VerificationLedger()

    assert ledger.status() == "not_required"
    evidence = ledger.record(
        kind="smoke",
        command=["python", "-c", "assert run()"],
        criteria=["最小行为检查通过"],
        success=True,
        returncode=0,
        summary="smoke check passed",
    )

    assert evidence.workspace_version == 0
    assert ledger.status() == "verified"
    ledger.mark_modified()
    assert ledger.status() == "unverified"
    assert ledger.summary()["stale_evidence_count"] == 1


def test_ledger_reports_partial_status_for_current_failed_and_successful_evidence() -> None:
    ledger = VerificationLedger()
    ledger.record(
        kind="build",
        command=["npm", "run", "build"],
        criteria=["项目可构建"],
        success=True,
        returncode=0,
        summary="build passed",
    )
    ledger.record(
        kind="smoke",
        command=["python", "check.py"],
        criteria=["行为检查"],
        success=False,
        returncode=1,
        summary="smoke failed",
    )

    assert ledger.status() == "partially_verified"
    assert ledger.summary()["current_evidence_count"] == 2


def test_execute_command_exposes_verification_metadata(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry(COMMAND_TOOLS))
    result = asyncio.run(
        executor.execute(
            ToolCall(
                id="verify-1",
                name="execute_command",
                arguments={
                    "command": ["python", "-c", "print('ok')"],
                    "verification_kind": "smoke",
                    "verification_criteria": ["最小行为检查通过"],
                },
            ),
            ToolContext(session_id="session-1", workspace=Workspace(tmp_path)),
        )
    )

    assert result.success is True
    assert result.output["verification"] == {
        "kind": "smoke",
        "criteria": ["最小行为检查通过"],
    }


class _Arguments(BaseModel):
    operation: str


class _EvidenceTool(Tool):
    name = "evidence_tool"
    description = "A deterministic test tool."
    parameters_model = _Arguments

    async def execute(self, context: ToolContext, arguments: _Arguments) -> ToolResult:
        if arguments.operation == "edit":
            return ToolResult(
                tool_call_id="pending",
                tool_name=self.name,
                success=True,
                output={"diff": "diff --git a/a.txt b/a.txt\n+changed"},
            )
        return ToolResult(
            tool_call_id="pending",
            tool_name=self.name,
            success=True,
            output={
                "command": ["check"],
                "returncode": 0,
                "stdout": "smoke passed",
                "verification": {"kind": "smoke", "criteria": ["行为通过"]},
            },
        )


def test_agent_final_event_contains_verification_and_stale_evidence(tmp_path: Path) -> None:
    events: list = []
    agent = Agent(
        MockModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="edit", name="evidence_tool", arguments={"operation": "edit"}),
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="verify", name="evidence_tool", arguments={"operation": "verify"}),
                    ]
                ),
                ModelResponse(content="完成"),
            ]
        ),
        ToolExecutor(ToolRegistry([_EvidenceTool()]), event_handler=events.append),
        ContextManager("You are a test agent."),
        Session(session_id="session-1", workspace_root=tmp_path),
        event_handler=events.append,
    )

    result = asyncio.run(agent.run("修改并验证"))

    assert result.verification["status"] == "verified"
    assert result.verification["workspace_version"] == 1
    updates = [event for event in events if event.type is AgentEventType.VERIFICATION_UPDATED]
    assert len(updates) == 2
    finished = next(event for event in events if event.type is AgentEventType.AGENT_FINISHED)
    assert finished.payload["verification"]["status"] == "verified"

