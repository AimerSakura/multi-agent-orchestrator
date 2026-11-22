"""MCPServer stdio server entrypoint exposing quota-locked Codex tools (`codex-worker`)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from dsh_mcp_bridge.codex_bridge import CodexConfig, CodexExecAdapter
from dsh_mcp_bridge.jobs import AsyncJobManager, JobRecord, clamp_sync_wait


def create_codex_server(
    config: CodexConfig | None = None,
    *,
    adapter: CodexExecAdapter | None = None,
) -> MCPServer:
    """Create and configure the MCPServer instance for codex-worker."""
    cfg = config or CodexConfig.from_env()
    exec_adapter = adapter or CodexExecAdapter(cfg)
    job_manager = AsyncJobManager()
    mcp = MCPServer("codex-worker")

    @mcp.tool()
    async def codex_health_check() -> dict[str, Any]:
        """Probe Codex CLI binary availability, default Luna model, and quota policy."""
        cli_status = await exec_adapter.check_health()
        return {
            "ready": bool(cli_status.get("available")),
            "backend": cfg.backend,
            "codex_cli": cli_status,
            "default_model": cfg.default_model,
            "allowed_models": list(cfg.allowed_models),
            "allow_escalation": cfg.allow_escalation,
            "allowed_root": str(cfg.allowed_root),
            "ignore_user_config": cfg.ignore_user_config,
            "max_sync_wait_sec": cfg.max_sync_wait_sec,
        }

    @mcp.tool()
    async def codex(
        prompt: str,
        cwd: str,
        sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
        model: str | None = None,
        worktree: bool = False,
        timeout_sec: int | None = None,
        wait_sec: int | None = None,
    ) -> dict[str, Any]:
        """Run a Codex session inside allowed_root with enforced Luna model allowlist."""
        effective_wait = clamp_sync_wait(wait_sec, cfg.max_sync_wait_sec)

        async def _run(record: JobRecord) -> dict[str, Any]:
            return await exec_adapter.run_session(
                prompt=prompt,
                cwd=cwd,
                sandbox=sandbox,
                model=model,
                worktree=worktree,
                timeout_sec=timeout_sec,
                on_thread_id=lambda tid: record.metadata.__setitem__("thread_id", tid),
            )

        return await job_manager.start_and_wait(
            tool_name="codex",
            poll_tool_name="codex_job_status",
            coro_factory=_run,
            metadata={"cwd": cwd, "model": model or cfg.default_model},
            wait_sec=effective_wait,
        )

    @mcp.tool(name="codex-reply")
    async def codex_reply(
        thread_id: str,
        prompt: str,
        cwd: str,
        model: str | None = None,
        sandbox: Literal["read-only", "workspace-write"] | None = None,
        timeout_sec: int | None = None,
        wait_sec: int | None = None,
    ) -> dict[str, Any]:
        """Continue an existing Codex thread by thread_id inside allowed_root."""
        effective_wait = clamp_sync_wait(wait_sec, cfg.max_sync_wait_sec)

        async def _run(record: JobRecord) -> dict[str, Any]:
            return await exec_adapter.reply_session(
                thread_id=thread_id,
                prompt=prompt,
                cwd=cwd,
                model=model,
                sandbox=sandbox,
                timeout_sec=timeout_sec,
                on_thread_id=lambda tid: record.metadata.__setitem__("thread_id", tid),
            )

        return await job_manager.start_and_wait(
            tool_name="codex-reply",
            poll_tool_name="codex_job_status",
            coro_factory=_run,
            metadata={"cwd": cwd, "thread_id": thread_id, "model": model or cfg.default_model},
            wait_sec=effective_wait,
        )

    @mcp.tool()
    async def codex_job_status(
        job_id: str | None = None,
        wait_sec: int = 120,
        cancel: bool = False,
    ) -> dict[str, Any]:
        """List background Codex jobs, long-poll a running job_id, or cancel it cleanly."""
        effective_wait = clamp_sync_wait(wait_sec, cfg.max_sync_wait_sec)
        return await job_manager.poll(
            job_id=job_id,
            wait_sec=effective_wait,
            cancel=cancel,
            poll_tool_name="codex_job_status",
        )

    @mcp.tool()
    async def codex_list_threads(
        cwd: str | None = None,
        limit: int = 20,
        include_exec: bool = False,
        search_term: str | None = None,
    ) -> dict[str, Any]:
        """List persistent Codex threads (Desktop/CLI interactive threads by default)."""
        return await exec_adapter.list_threads(
            cwd=cwd,
            limit=limit,
            include_exec=include_exec,
            search_term=search_term,
        )

    @mcp.tool()
    async def codex_read_thread(
        thread_id: str,
        max_turns: int = 20,
        max_chars_per_message: int = 4000,
    ) -> dict[str, Any]:
        """Read full turn and message history from a persistent Codex thread."""
        return await exec_adapter.read_thread(
            thread_id=thread_id,
            max_turns=max_turns,
            max_chars_per_message=max_chars_per_message,
        )

    return mcp


def main() -> None:
    """Run the codex-mcp-bridge MCP server over stdio."""
    server = create_codex_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
