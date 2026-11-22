"""MCPServer stdio server entrypoint and tool definitions for dsh-mcp-bridge."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from dsh_mcp_bridge.adapters import (
    DeepSeekApiFallbackAdapter,
    DshCliAdapter,
    DshHttpAdapter,
    ResumeTaskRequest,
    RunTaskRequest,
    TaskResult,
)
from dsh_mcp_bridge.adapters.dsh_cli import wrap_command_for_platform
from dsh_mcp_bridge.codex_bridge import discover_codex_executable
from dsh_mcp_bridge.config import BridgeConfig, VALID_MODEL_TIERS
from dsh_mcp_bridge.dsh_sessions import list_dsh_sessions, read_dsh_session, resolve_dsh_home
from dsh_mcp_bridge.logger import TrajectoryRecord, TrajectoryStore, get_logger
from dsh_mcp_bridge.security import (
    PathSecurityError,
    validate_output_file,
    validate_workspace_dir,
)

_LOGGER = get_logger("dsh_mcp_bridge.server")


async def _probe_codex_cli() -> dict[str, Any]:
    executable = discover_codex_executable()
    if not executable:
        return {"available": False, "executable": None, "version": None}

    try:
        cmd = wrap_command_for_platform([executable, "--version"])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        version = stdout_b.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else None
        return {"available": proc.returncode == 0, "executable": executable, "version": version}
    except Exception as exc:
        return {"available": False, "executable": executable, "version": None, "error": str(exc)}


def _record_result(
    store: TrajectoryStore,
    result: TaskResult,
    workspace_dir: Path,
    model_tier: str,
) -> None:
    if not result.trajectory_id:
        return
    store.upsert(
        TrajectoryRecord(
            trajectory_id=result.trajectory_id,
            workspace_dir=str(workspace_dir),
            adapter=result.adapter,
            model_tier=model_tier,
            status=result.status,
            summary=result.summary,
            artifacts=result.artifacts,
            parent_trajectory_id=result.parent_trajectory_id,
        )
    )


def create_server(
    config: BridgeConfig | None = None,
    *,
    cli_adapter: DshCliAdapter | None = None,
    http_adapter: DshHttpAdapter | None = None,
    api_adapter: DeepSeekApiFallbackAdapter | None = None,
    store: TrajectoryStore | None = None,
) -> MCPServer:
    """Create and configure the MCPServer instance with all dsh bridge tools."""
    cfg = config or BridgeConfig.from_env()
    cli = cli_adapter or DshCliAdapter(cfg)
    http = http_adapter or DshHttpAdapter(cfg)
    api = api_adapter or DeepSeekApiFallbackAdapter(cfg)
    trajectory_store = store or TrajectoryStore(cfg.allowed_root)

    mcp = MCPServer("dsh-mcp-bridge")

    @mcp.tool()
    async def dsh_health_check() -> dict[str, Any]:
        """Probe local `dsh` CLI, `codex` CLI, HTTP port 3080, and fallback API status."""
        cli_status, http_status, codex_status = await asyncio.gather(
            cli.check_health(),
            http.check_health(),
            _probe_codex_cli(),
        )
        api_status = await api.check_health()
        ready = bool(cli_status.get("available") or api.is_configured())
        return {
            "ready": ready,
            "dsh_cli": cli_status,
            "dsh_http": http_status,
            "codex_cli": codex_status,
            "deepseek_api_key_configured": bool(cfg.deepseek_api_key),
            "allow_api_fallback": cfg.allow_api_fallback,
            "api_fallback": api_status,
            "allowed_root": str(cfg.allowed_root),
            "dsh_home": cfg.dsh_home,
        }

    @mcp.tool()
    async def dsh_run_task(
        task_prompt: str,
        workspace_dir: str,
        output_file: str | None = None,
        model_tier: Literal["flash", "reasoner", "default"] = "flash",
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Execute a task via DeepSeek Harness (`dsh`) inside the allowed workspace."""
        if not task_prompt or not task_prompt.strip():
            return TaskResult(
                status="failed",
                trajectory_id="",
                summary="task_prompt must not be empty.",
                adapter="dsh-cli",
            ).to_dict()

        if model_tier not in VALID_MODEL_TIERS:
            return TaskResult(
                status="failed",
                trajectory_id="",
                summary=f"Invalid model_tier '{model_tier}'. Expected one of {VALID_MODEL_TIERS}.",
                adapter="dsh-cli",
            ).to_dict()

        try:
            validated_workspace = validate_workspace_dir(workspace_dir, cfg.allowed_root)
            validated_output = validate_output_file(output_file, validated_workspace, cfg.allowed_root)
        except PathSecurityError as exc:
            return TaskResult(
                status="failed",
                trajectory_id="",
                summary=f"Security validation failed: {exc}",
                logs_tail=str(exc),
                adapter="dsh-cli",
            ).to_dict()

        effective_timeout = cfg.resolve_timeout(timeout_sec)
        request = RunTaskRequest(
            task_prompt=task_prompt,
            workspace_dir=validated_workspace,
            output_file=validated_output,
            model_tier=model_tier,
            timeout_sec=effective_timeout,
        )

        if cli.find_executable():
            result = await cli.run_task(request)
        elif api.is_configured():
            _LOGGER.info("dsh CLI unavailable; routing task through DeepSeek API fallback.")
            result = await api.run_task(request)
        else:
            return TaskResult(
                status="failed",
                trajectory_id="",
                summary=(
                    f"dsh CLI executable '{cfg.dsh_bin}' was not found in PATH "
                    "and DeepSeek API fallback is disabled."
                ),
                logs_tail="Install dsh or set DSH_ALLOW_API_FALLBACK=true with DEEPSEEK_API_KEY.",
                adapter="dsh-cli",
            ).to_dict()

        _record_result(trajectory_store, result, validated_workspace, model_tier)
        return result.to_dict()

    @mcp.tool()
    async def dsh_resume_or_fork(
        trajectory_id: str,
        mode: Literal["resume", "fork"] = "resume",
        new_instruction: str = "",
        workspace_dir: str | None = None,
        model_tier: Literal["flash", "reasoner", "default"] = "default",
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Resume an existing dsh session or fork a new branch seeded from prior context."""
        clean_id = trajectory_id.strip()
        if not clean_id:
            return TaskResult(
                status="failed",
                trajectory_id="",
                summary="trajectory_id must not be empty.",
                adapter="dsh-cli",
            ).to_dict()

        if mode not in ("resume", "fork"):
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=f"Invalid mode '{mode}'. Expected 'resume' or 'fork'.",
                adapter="dsh-cli",
            ).to_dict()

        if model_tier not in VALID_MODEL_TIERS:
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=f"Invalid model_tier '{model_tier}'. Expected one of {VALID_MODEL_TIERS}.",
                adapter="dsh-cli",
            ).to_dict()

        parent_record = trajectory_store.get(clean_id)
        raw_workspace = workspace_dir or (parent_record.workspace_dir if parent_record else None)
        if not raw_workspace:
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=(
                    f"Trajectory '{clean_id}' was not found in bridge storage; "
                    "pass workspace_dir explicitly to resume or fork it."
                ),
                adapter="dsh-cli",
            ).to_dict()

        try:
            validated_workspace = validate_workspace_dir(raw_workspace, cfg.allowed_root)
        except PathSecurityError as exc:
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=f"Security validation failed: {exc}",
                logs_tail=str(exc),
                adapter="dsh-cli",
            ).to_dict()

        if mode == "resume" and parent_record is not None and parent_record.adapter == "ds-api":
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=(
                    f"Trajectory '{clean_id}' originated from DeepSeek API fallback ('ds-api'), "
                    "which does not have a native dsh session; use mode='fork' instead."
                ),
                adapter="dsh-cli",
            ).to_dict()

        if not cli.find_executable():
            return TaskResult(
                status="failed",
                trajectory_id=clean_id,
                summary=f"dsh CLI executable '{cfg.dsh_bin}' was not found in PATH.",
                adapter="dsh-cli",
            ).to_dict()

        request = ResumeTaskRequest(
            trajectory_id=clean_id,
            mode=mode,
            new_instruction=new_instruction,
            workspace_dir=validated_workspace,
            model_tier=model_tier,
            timeout_sec=cfg.resolve_timeout(timeout_sec),
            parent_record=parent_record,
        )
        result = await cli.resume_or_fork(request)
        _record_result(trajectory_store, result, validated_workspace, model_tier)
        return result.to_dict()

    @mcp.tool()
    async def dsh_list_sessions(
        workspace_dir: str | None = None,
        limit: int = 20,
        search_term: str | None = None,
    ) -> dict[str, Any]:
        """List native DeepSeek Harness sessions from DSH storage."""
        dsh_home = resolve_dsh_home(cfg.dsh_home)
        return await asyncio.to_thread(
            list_dsh_sessions,
            dsh_home,
            workspace_dir=workspace_dir,
            limit=limit,
            search_term=search_term,
        )

    @mcp.tool()
    async def dsh_read_session(
        session_id: str,
        max_turns: int = 20,
        max_chars_per_message: int = 4000,
    ) -> dict[str, Any]:
        """Read native DeepSeek Harness conversation turns from session storage."""
        dsh_home = resolve_dsh_home(cfg.dsh_home)
        return await asyncio.to_thread(
            read_dsh_session,
            dsh_home,
            session_id=session_id,
            max_turns=max_turns,
            max_chars_per_message=max_chars_per_message,
        )

    return mcp


def main() -> None:
    """Run the dsh-mcp-bridge MCP server over stdio."""
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
