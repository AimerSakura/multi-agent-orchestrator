"""Optional DeepSeek official API fallback adapter for pure-reasoning tasks."""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import httpx

from dsh_mcp_bridge.config import BridgeConfig
from dsh_mcp_bridge.logger import truncate_tail

from .base import RunTaskRequest, TaskResult


class DeepSeekApiFallbackAdapter:
    """Pure-reasoning HTTP fallback calling the DeepSeek Chat Completions API.

    Used only when explicitly enabled (`DSH_ALLOW_API_FALLBACK=true` and
    `DEEPSEEK_API_KEY` configured) and local `dsh` CLI is unavailable.
    """

    def __init__(self, config: BridgeConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    def is_configured(self) -> bool:
        """Return True when API fallback is enabled and an API key is available."""
        return bool(self.config.allow_api_fallback and self.config.deepseek_api_key)

    @staticmethod
    def _api_model_for_tier(model_tier: str) -> str:
        if model_tier.strip().lower() == "reasoner":
            return "deepseek-reasoner"
        return "deepseek-chat"

    async def check_health(self) -> dict[str, Any]:
        """Report whether the DeepSeek API fallback channel is configured."""
        return {
            "enabled": self.config.allow_api_fallback,
            "api_key_configured": bool(self.config.deepseek_api_key),
            "api_base": self.config.deepseek_api_base,
        }

    async def run_task(self, request: RunTaskRequest) -> TaskResult:
        """Execute a pure-reasoning completion against the DeepSeek API."""
        trajectory_id = f"api-{uuid4().hex[:12]}"
        if not self.config.deepseek_api_key:
            return TaskResult(
                status="failed",
                trajectory_id=trajectory_id,
                summary="DEEPSEEK_API_KEY is not configured for API fallback.",
                artifacts=[],
                logs_tail="",
                adapter="ds-api",
            )

        model = self._api_model_for_tier(request.model_tier)
        endpoint = f"{self.config.deepseek_api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are DeepSeek pure-reasoning fallback for dsh-mcp-bridge. "
                        f"Target workspace: {request.workspace_dir}. "
                        "Provide clear, structured analysis without inventing local file edits."
                    ),
                },
                {"role": "user", "content": request.task_prompt},
            ],
        }

        try:
            async with httpx.AsyncClient(
                timeout=float(request.timeout_sec),
                transport=self._transport,
            ) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException:
            return TaskResult(
                status="timeout",
                trajectory_id=trajectory_id,
                summary=f"DeepSeek API request timed out after {request.timeout_sec}s.",
                artifacts=[],
                logs_tail="",
                adapter="ds-api",
            )
        except Exception as exc:
            return TaskResult(
                status="failed",
                trajectory_id=trajectory_id,
                summary=f"DeepSeek API fallback request failed: {exc}",
                artifacts=[],
                logs_tail=truncate_tail(str(exc), self.config.max_log_chars),
                adapter="ds-api",
            )

        content = self._extract_content(body)
        artifacts: list[str] = []
        if request.output_file is not None and content:
            request.output_file.parent.mkdir(parents=True, exist_ok=True)
            request.output_file.write_text(content, encoding="utf-8")
            artifacts.append(os.path.abspath(str(request.output_file)))

        usage = self._extract_usage(body.get("usage"))
        return TaskResult(
            status="success",
            trajectory_id=trajectory_id,
            summary=content or "Completed with empty response.",
            artifacts=artifacts,
            logs_tail=f"Completed via DeepSeek API fallback ({model}).",
            adapter="ds-api",
            usage=usage,
        )

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return str(content) if content is not None else ""

    @staticmethod
    def _extract_usage(raw_usage: Any) -> dict[str, int] | None:
        if not isinstance(raw_usage, dict):
            return None
        mapped: dict[str, int] = {}
        key_map = {
            "prompt_tokens": "inputTokens",
            "completion_tokens": "outputTokens",
            "total_tokens": "totalTokens",
        }
        for src, dst in key_map.items():
            val = raw_usage.get(src)
            if isinstance(val, int):
                mapped[dst] = val
        return mapped or None
