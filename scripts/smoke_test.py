"""Live MCP stdio smoke test for dsh-mcp-bridge and codex-mcp-bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _extract_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed

    raise AssertionError(f"Unable to parse structured dict from MCP tool result: {result!r}")


async def verify_dsh_bridge(repo_root: Path) -> dict[str, Any]:
    """Connect to dsh-mcp-bridge over stdio and verify tool discovery, health, and security."""
    env = os.environ.copy()
    env["DSH_ALLOWED_ROOT"] = str(repo_root)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dsh_mcp_bridge.server"],
        env=env,
        cwd=str(repo_root),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=15.0)

            tools_response = await asyncio.wait_for(session.list_tools(), timeout=15.0)
            tool_names = {tool.name for tool in tools_response.tools}
            expected = {
                "dsh_health_check",
                "dsh_run_task",
                "dsh_resume_or_fork",
                "dsh_list_sessions",
                "dsh_read_session",
            }
            missing = expected - tool_names
            assert not missing, f"Missing expected dsh-mcp-bridge tools: {sorted(missing)}"

            health_res = await asyncio.wait_for(session.call_tool("dsh_health_check", {}), timeout=15.0)
            health_payload = _extract_payload(health_res)
            for key in ("ready", "dsh_cli", "dsh_http", "codex_cli", "allowed_root"):
                assert key in health_payload, f"Missing '{key}' in health payload: {health_payload}"
            assert Path(health_payload["allowed_root"]).resolve() == repo_root.resolve()

            outside_dir = "C:\\Windows" if os.name == "nt" else "/etc"
            sec_res = await asyncio.wait_for(
                session.call_tool(
                    "dsh_run_task",
                    {
                        "task_prompt": "smoke security check",
                        "workspace_dir": outside_dir,
                    },
                ),
                timeout=15.0,
            )
            sec_payload = _extract_payload(sec_res)
            assert sec_payload.get("status") == "failed", f"Expected security failure, got: {sec_payload}"
            assert "Security validation failed" in sec_payload.get("summary", ""), (
                f"Expected security rejection summary, got: {sec_payload}"
            )
            return health_payload


async def verify_codex_worker(repo_root: Path) -> dict[str, Any]:
    """Connect to codex-mcp-bridge over stdio and verify tools, health, quota lock, and path guard."""
    env = os.environ.copy()
    env["CODEX_ALLOWED_ROOT"] = str(repo_root)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dsh_mcp_bridge.codex_server"],
        env=env,
        cwd=str(repo_root),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=15.0)

            tools_response = await asyncio.wait_for(session.list_tools(), timeout=15.0)
            tool_names = {tool.name for tool in tools_response.tools}
            expected_tools = {
                "codex",
                "codex-reply",
                "codex_health_check",
                "codex_list_threads",
                "codex_read_thread",
            }
            missing = expected_tools - tool_names
            assert not missing, f"Missing expected codex-mcp-bridge tools: {sorted(missing)}"

            health_res = await asyncio.wait_for(session.call_tool("codex_health_check", {}), timeout=15.0)
            health_payload = _extract_payload(health_res)
            assert health_payload["codex_cli"]["available"] is True, (
                f"Codex CLI not available: {health_payload}"
            )
            assert health_payload.get("default_model") == "gpt-6-luna", health_payload

            quota_res = await asyncio.wait_for(
                session.call_tool(
                    "codex",
                    {
                        "prompt": "quota guard check",
                        "cwd": str(repo_root),
                        "model": "gpt-6.2-sol",
                    },
                ),
                timeout=15.0,
            )
            quota_payload = _extract_payload(quota_res)
            assert quota_payload.get("status") == "failed", f"Expected quota rejection, got: {quota_payload}"
            assert "blocked by quota policy" in quota_payload.get("summary", ""), quota_payload

            outside_dir = "C:\\Windows" if os.name == "nt" else "/etc"
            path_res = await asyncio.wait_for(
                session.call_tool(
                    "codex",
                    {
                        "prompt": "path guard check",
                        "cwd": outside_dir,
                    },
                ),
                timeout=15.0,
            )
            path_payload = _extract_payload(path_res)
            assert path_payload.get("status") == "failed", f"Expected path rejection, got: {path_payload}"
            assert "Security validation failed" in path_payload.get("summary", ""), path_payload

            return {
                "tools": sorted(expected_tools),
                "codex_version": health_payload["codex_cli"].get("version"),
                "default_model": health_payload.get("default_model"),
                "backend": health_payload.get("backend"),
            }


async def run_smoke_test(*, skip_codex: bool = False) -> None:
    """Run live MCP stdio checks for dsh-mcp-bridge and optionally codex-mcp-bridge."""
    repo_root = Path(__file__).resolve().parent.parent
    health_payload = await verify_dsh_bridge(repo_root)

    print("MCP smoke test [dsh-mcp-bridge] PASSED:")
    print(
        "  Tools verified : "
        "['dsh_health_check', 'dsh_list_sessions', 'dsh_read_session', 'dsh_resume_or_fork', 'dsh_run_task']"
    )
    print(f"  dsh_cli avail  : {health_payload['dsh_cli'].get('available')}")
    print(f"  dsh_http reach : {health_payload['dsh_http'].get('reachable')}")
    print(
        f"  codex_cli avail: {health_payload['codex_cli'].get('available')} "
        f"({health_payload['codex_cli'].get('version')})"
    )
    print("  Whitelist guard: PASS (rejected outside workspace cleanly)")

    if skip_codex:
        print("MCP smoke test [codex-worker] SKIPPED (--skip-codex)")
        return

    codex_info = await verify_codex_worker(repo_root)
    print("MCP smoke test [codex-worker / codex-mcp-bridge] PASSED:")
    print(f"  Tools verified : {codex_info['tools']}")
    print(f"  Backend        : {codex_info['backend']}")
    print(f"  Codex CLI      : {codex_info['codex_version']}")
    print(f"  Default model  : {codex_info['default_model']}")
    print("  Quota guard    : PASS (blocked gpt-6.2-sol cleanly)")
    print("  Whitelist guard: PASS (rejected outside workspace cleanly)")


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for smoke test CLI."""
    parser = argparse.ArgumentParser(description="Live MCP stdio smoke test for orchestrator servers.")
    parser.add_argument(
        "--skip-codex",
        action="store_true",
        help="Skip live handshake verification for codex-mcp-bridge.",
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(run_smoke_test(skip_codex=args.skip_codex))
        return 0
    except Exception as exc:
        print(f"MCP smoke test FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
