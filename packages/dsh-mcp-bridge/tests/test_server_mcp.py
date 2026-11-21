"""Tests for MCPServer tool registration, health check, API fallback, and resume/fork."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dsh_mcp_bridge.adapters.base import ResumeTaskRequest, RunTaskRequest, TaskResult
from dsh_mcp_bridge.adapters.ds_api import DeepSeekApiFallbackAdapter
from dsh_mcp_bridge.adapters.dsh_cli import DshCliAdapter
from dsh_mcp_bridge.adapters.dsh_http import DshHttpAdapter
from dsh_mcp_bridge.config import BridgeConfig
from dsh_mcp_bridge.logger import TrajectoryRecord, TrajectoryStore
from dsh_mcp_bridge.server import create_server


def _extract_tool_dict(call_result: Any) -> dict[str, Any]:
    if isinstance(call_result, tuple):
        for part in call_result:
            if isinstance(part, dict):
                return part
        content_list = call_result[0]
    else:
        structured = getattr(call_result, "structured_content", None)
        if not isinstance(structured, dict):
            structured = getattr(call_result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content_list = getattr(call_result, "content", call_result)

    if isinstance(content_list, dict):
        return content_list
    if isinstance(content_list, list) and content_list:
        text = getattr(content_list[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise AssertionError(f"Could not extract dict from MCPServer tool output: {call_result!r}")


class _MockCliAdapter(DshCliAdapter):
    def __init__(self, config: BridgeConfig, *, available: bool = True) -> None:
        super().__init__(config)
        self._available = available
        self.last_run: RunTaskRequest | None = None
        self.last_resume: ResumeTaskRequest | None = None

    def find_executable(self) -> str | None:
        return "/fake/dsh" if self._available else None

    async def check_health(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "executable": "/fake/dsh" if self._available else None,
            "version": "0.2.0-rc.2" if self._available else None,
            "error": None if self._available else "not found",
        }

    async def run_task(self, request: RunTaskRequest) -> TaskResult:
        self.last_run = request
        return TaskResult(
            status="success",
            trajectory_id="traj-001",
            summary="CLI mock completed",
            artifacts=[str(request.output_file)] if request.output_file else [],
            logs_tail="ok",
            adapter="dsh-cli",
            exit_code=0,
        )

    async def resume_or_fork(self, request: ResumeTaskRequest) -> TaskResult:
        self.last_resume = request
        new_id = request.trajectory_id if request.mode == "resume" else "traj-002-fork"
        return TaskResult(
            status="success",
            trajectory_id=new_id,
            summary=f"{request.mode} completed",
            artifacts=[],
            logs_tail="ok",
            adapter="dsh-cli",
            parent_trajectory_id=request.trajectory_id if request.mode == "fork" else None,
        )


@pytest.mark.asyncio
async def test_server_registers_all_tools_and_health_hides_secret(tmp_path: Path) -> None:
    secret = "sk-super-secret-token-value"
    cfg = BridgeConfig(
        allowed_root=tmp_path,
        deepseek_api_key=secret,
        allow_api_fallback=False,
    )
    http_transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    server = create_server(
        cfg,
        cli_adapter=_MockCliAdapter(cfg, available=True),
        http_adapter=DshHttpAdapter(cfg, transport=http_transport),
        api_adapter=DeepSeekApiFallbackAdapter(cfg),
    )

    tools = await server.list_tools()
    assert {t.name for t in tools} == {"dsh_health_check", "dsh_run_task", "dsh_resume_or_fork"}

    raw_health = await server.call_tool("dsh_health_check", {})
    health = _extract_tool_dict(raw_health)
    assert health["ready"] is True
    assert health["dsh_cli"]["available"] is True
    assert health["dsh_http"]["reachable"] is True
    assert "codex_cli" in health
    assert health["deepseek_api_key_configured"] is True
    assert secret not in json.dumps(health)


@pytest.mark.asyncio
async def test_server_run_task_rejects_outside_workspace_and_records_valid_run(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    cfg = BridgeConfig(allowed_root=allowed)
    cli = _MockCliAdapter(cfg, available=True)
    store = TrajectoryStore(allowed)
    server = create_server(cfg, cli_adapter=cli, store=store)

    rejected = _extract_tool_dict(
        await server.call_tool(
            "dsh_run_task",
            {"task_prompt": "do work", "workspace_dir": str(outside)},
        )
    )
    assert rejected["status"] == "failed"
    assert "Security validation failed" in rejected["summary"]

    accepted = _extract_tool_dict(
        await server.call_tool(
            "dsh_run_task",
            {
                "task_prompt": "clean csv",
                "workspace_dir": str(allowed),
                "output_file": ".agent-handoff/out.json",
                "model_tier": "flash",
            },
        )
    )
    assert accepted["status"] == "success"
    assert accepted["trajectory_id"] == "traj-001"
    saved = store.get("traj-001")
    assert saved is not None
    assert saved.workspace_dir == str(allowed.resolve())


@pytest.mark.asyncio
async def test_server_api_fallback_writes_output_file(tmp_path: Path) -> None:
    cfg = BridgeConfig(
        allowed_root=tmp_path,
        deepseek_api_key="sk-test",
        allow_api_fallback=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Analysis\nAll good"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    api = DeepSeekApiFallbackAdapter(cfg, transport=httpx.MockTransport(handler))
    cli = _MockCliAdapter(cfg, available=False)
    server = create_server(cfg, cli_adapter=cli, api_adapter=api)

    out_rel = ".agent-handoff/t1/analysis.md"
    result = _extract_tool_dict(
        await server.call_tool(
            "dsh_run_task",
            {
                "task_prompt": "analyze schema",
                "workspace_dir": str(tmp_path),
                "output_file": out_rel,
                "model_tier": "reasoner",
            },
        )
    )

    assert result["status"] == "success"
    assert result["adapter"] == "ds-api"
    assert result["trajectory_id"].startswith("api-")
    expected_file = (tmp_path / out_rel).resolve()
    assert expected_file.read_text(encoding="utf-8") == "# Analysis\nAll good"
    assert str(expected_file) in result["artifacts"]


@pytest.mark.asyncio
async def test_server_resume_or_fork_uses_trajectory_store_workspace(tmp_path: Path) -> None:
    cfg = BridgeConfig(allowed_root=tmp_path)
    cli = _MockCliAdapter(cfg, available=True)
    store = TrajectoryStore(allowed_root=tmp_path)
    store.upsert(
        TrajectoryRecord(
            trajectory_id="sess-stored",
            workspace_dir=str(tmp_path),
            adapter="dsh-cli",
            model_tier="flash",
            status="success",
            summary="initial pass",
            artifacts=[],
        )
    )
    server = create_server(cfg, cli_adapter=cli, store=store)

    resumed = _extract_tool_dict(
        await server.call_tool(
            "dsh_resume_or_fork",
            {
                "trajectory_id": "sess-stored",
                "mode": "resume",
                "new_instruction": "polish",
            },
        )
    )
    assert resumed["status"] == "success"
    assert resumed["trajectory_id"] == "sess-stored"
    assert cli.last_resume is not None
    assert cli.last_resume.workspace_dir == tmp_path.resolve()

    forked = _extract_tool_dict(
        await server.call_tool(
            "dsh_resume_or_fork",
            {
                "trajectory_id": "sess-stored",
                "mode": "fork",
                "new_instruction": "try branch B",
            },
        )
    )
    assert forked["status"] == "success"
    assert forked["trajectory_id"] == "traj-002-fork"
    assert forked["parent_trajectory_id"] == "sess-stored"
    assert store.get("traj-002-fork") is not None
