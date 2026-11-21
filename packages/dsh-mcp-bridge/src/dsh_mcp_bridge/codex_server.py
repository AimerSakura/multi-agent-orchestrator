"""MCPServer stdio server entrypoint exposing quota-locked Codex tools (`codex-worker`)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from dsh_mcp_bridge.codex_bridge import CodexConfig, CodexExecAdapter


def create_codex_server(
    config: CodexConfig | None = None,
    *,
    adapter: CodexExecAdapter | None = None,
) -> MCPServer:
    """Create and configure the MCPServer instance for codex-worker."""
    cfg = config or CodexConfig.from_env()
    exec_adapter = adapter or CodexExecAdapter(cfg)
    mcp = MCPServer("codex-worker")

    @mcp.tool()
    async def codex_health_check() -> dict[str, Any]:
        """Probe Codex CLI binary availability, default Luna model, and quota policy."""
        cli_status = await exec_adapter.check_health()
        return {
            "ready": bool(cli_status.get("available")),
            "codex_cli": cli_status,
            "default_model": cfg.default_model,
            "allowed_models": list(cfg.allowed_models),
            "allow_escalation": cfg.allow_escalation,
            "allowed_root": str(cfg.allowed_root),
            "ignore_user_config": cfg.ignore_user_config,
        }

    @mcp.tool()
    async def codex(
        prompt: str,
        cwd: str,
        sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
        model: str | None = None,
        worktree: bool = False,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Run a Codex session inside allowed_root with enforced Luna model allowlist."""
        return await exec_adapter.run_session(
            prompt=prompt,
            cwd=cwd,
            sandbox=sandbox,
            model=model,
            worktree=worktree,
            timeout_sec=timeout_sec,
        )

    @mcp.tool(name="codex-reply")
    async def codex_reply(
        thread_id: str,
        prompt: str,
        cwd: str,
        model: str | None = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Continue an existing Codex thread by thread_id inside allowed_root."""
        return await exec_adapter.reply_session(
            thread_id=thread_id,
            prompt=prompt,
            cwd=cwd,
            model=model,
            timeout_sec=timeout_sec,
        )

    return mcp


def main() -> None:
    """Run the codex-mcp-bridge MCP server over stdio."""
    server = create_codex_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
