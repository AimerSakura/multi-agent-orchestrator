"""Local DeepSeek Harness HTTP endpoint probe adapter."""

from __future__ import annotations

from typing import Any

import httpx

from dsh_mcp_bridge.config import BridgeConfig


class DshHttpAdapter:
    """Probes the local `dsh --profile web` server (default http://127.0.0.1:3080).

    Note: In dsh 0.2.0-rc.2, the web profile serves a browser UI protected by a
    `/api` browser-trust fence; CLI headless mode is therefore the primary execution
    channel while this adapter monitors local web server availability.
    """

    def __init__(self, config: BridgeConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    async def check_health(self, timeout_sec: float = 2.0) -> dict[str, Any]:
        """Check whether the configured local dsh HTTP endpoint responds."""
        url = self.config.dsh_base_url
        try:
            async with httpx.AsyncClient(timeout=timeout_sec, transport=self._transport) as client:
                response = await client.get(url)
            return {
                "reachable": True,
                "url": url,
                "status_code": response.status_code,
                "error": None,
            }
        except Exception as exc:
            return {
                "reachable": False,
                "url": url,
                "status_code": None,
                "error": str(exc),
            }
