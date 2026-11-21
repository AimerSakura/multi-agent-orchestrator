"""Live MCP stdio smoke test for dsh-mcp-bridge."""

from __future__ import annotations

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


async def run_smoke_test() -> None:
    """Connect to dsh-mcp-bridge over stdio and verify tool discovery, health, and security."""
    repo_root = Path(__file__).resolve().parent.parent
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
            await session.initialize()

            tools_response = await session.list_tools()
            tool_names = {tool.name for tool in tools_response.tools}
            expected = {"dsh_health_check", "dsh_run_task", "dsh_resume_or_fork"}
            missing = expected - tool_names
            assert not missing, f"Missing expected MCP tools: {sorted(missing)}"

            health_res = await session.call_tool("dsh_health_check", {})
            health_payload = _extract_payload(health_res)
            for key in ("ready", "dsh_cli", "dsh_http", "allowed_root"):
                assert key in health_payload, f"Missing '{key}' in health payload: {health_payload}"
            assert Path(health_payload["allowed_root"]).resolve() == repo_root.resolve()

            outside_dir = "C:\\Windows" if os.name == "nt" else "/etc"
            sec_res = await session.call_tool(
                "dsh_run_task",
                {
                    "task_prompt": "smoke security check",
                    "workspace_dir": outside_dir,
                },
            )
            sec_payload = _extract_payload(sec_res)
            assert sec_payload.get("status") == "failed", f"Expected security failure, got: {sec_payload}"
            assert "Security validation failed" in sec_payload.get("summary", ""), (
                f"Expected security rejection summary, got: {sec_payload}"
            )

            print("MCP smoke test PASSED:")
            print(f"  Tools verified : {sorted(expected)}")
            print(f"  dsh_cli avail  : {health_payload['dsh_cli'].get('available')}")
            print(f"  dsh_http reach : {health_payload['dsh_http'].get('reachable')}")
            print("  Whitelist guard: PASS (rejected outside workspace cleanly)")


def main() -> int:
    """Entrypoint for smoke test CLI."""
    try:
        asyncio.run(run_smoke_test())
        return 0
    except Exception as exc:
        print(f"MCP smoke test FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
