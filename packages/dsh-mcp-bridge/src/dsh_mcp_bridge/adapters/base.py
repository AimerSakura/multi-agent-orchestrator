"""Shared request/response models and adapter protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from dsh_mcp_bridge.logger import TrajectoryRecord

TaskStatus = Literal["success", "failed", "timeout"]
ResumeMode = Literal["resume", "fork"]


@dataclass(frozen=True, slots=True)
class RunTaskRequest:
    """Normalized request passed to an execution adapter for a new task."""

    task_prompt: str
    workspace_dir: Path
    output_file: Path | None = None
    model_tier: str = "flash"
    timeout_sec: int = 300


@dataclass(frozen=True, slots=True)
class ResumeTaskRequest:
    """Normalized request to resume or fork an existing trajectory."""

    trajectory_id: str
    mode: ResumeMode
    new_instruction: str
    workspace_dir: Path
    model_tier: str = "default"
    timeout_sec: int = 300
    parent_record: TrajectoryRecord | None = None


@dataclass(slots=True)
class TaskResult:
    """Structured outcome returned by MCP tools to the orchestrator."""

    status: TaskStatus
    trajectory_id: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    logs_tail: str = ""
    adapter: str = "dsh-cli"
    exit_code: int | None = None
    parent_trajectory_id: str | None = None
    usage: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize into the MCP tool JSON response payload."""
        payload: dict[str, Any] = {
            "status": self.status,
            "trajectory_id": self.trajectory_id,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "logs_tail": self.logs_tail,
            "adapter": self.adapter,
        }
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.parent_trajectory_id is not None:
            payload["parent_trajectory_id"] = self.parent_trajectory_id
        if self.usage is not None:
            payload["usage"] = self.usage
        return payload


class ExecutionAdapter(Protocol):
    """Interface implemented by dsh execution adapters."""

    async def check_health(self) -> dict[str, Any]:
        """Probe adapter availability and configuration health."""
        ...

    async def run_task(self, request: RunTaskRequest) -> TaskResult:
        """Execute a new task and return structured results."""
        ...
