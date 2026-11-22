"""Bounded-wait async background job manager for long-running MCP tool calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine
from uuid import uuid4

from dsh_mcp_bridge.logger import utc_now_iso

DEFAULT_SYNC_WAIT_SEC = 120
MAX_SYNC_WAIT_SEC = 150


def clamp_sync_wait(wait_sec: int | float | None, default_wait: int = DEFAULT_SYNC_WAIT_SEC) -> float:
    """Clamp synchronous wait duration strictly within [0.0, MAX_SYNC_WAIT_SEC]."""
    if wait_sec is None:
        return float(max(0, min(int(default_wait), MAX_SYNC_WAIT_SEC)))
    return max(0.0, min(float(wait_sec), float(MAX_SYNC_WAIT_SEC)))


@dataclass(slots=True)
class JobRecord:
    """In-memory handle tracking one asynchronous MCP tool execution."""

    job_id: str
    tool_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at_iso: str = field(default_factory=utc_now_iso)
    created_at_monotonic: float = field(default_factory=time.monotonic)
    finished_at_monotonic: float | None = None
    status: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[dict[str, Any]] | None = None

    def elapsed_sec(self) -> float:
        """Return elapsed execution time in seconds."""
        end = self.finished_at_monotonic if self.finished_at_monotonic is not None else time.monotonic()
        return max(0.0, end - self.created_at_monotonic)


class AsyncJobManager:
    """Detaches slow worker runs before Antigravity's 180s MCP RPC timeout hits."""

    def __init__(self, *, max_history: int = 50) -> None:
        self._max_history = max(1, int(max_history))
        self._jobs: dict[str, JobRecord] = {}

    def _prune_finished(self) -> None:
        finished_ids = [jid for jid, rec in self._jobs.items() if rec.status != "running"]
        excess = len(finished_ids) - self._max_history
        if excess > 0:
            for jid in finished_ids[:excess]:
                del self._jobs[jid]

    async def start_and_wait(
        self,
        *,
        tool_name: str,
        poll_tool_name: str,
        coro_factory: Callable[[JobRecord], Coroutine[Any, Any, dict[str, Any]]],
        metadata: dict[str, Any] | None = None,
        wait_sec: float = DEFAULT_SYNC_WAIT_SEC,
    ) -> dict[str, Any]:
        """Launch `coro_factory` as a background task and wait up to `wait_sec` seconds."""
        record = JobRecord(
            job_id=f"job-{uuid4().hex[:10]}",
            tool_name=tool_name,
            metadata=dict(metadata or {}),
        )
        self._jobs[record.job_id] = record
        self._prune_finished()

        async def _runner() -> dict[str, Any]:
            try:
                res = await coro_factory(record)
                record.result = res
                record.status = "completed"
                return res
            except asyncio.CancelledError:
                record.status = "cancelled"
                record.error = "Cancelled by caller."
                raise
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                record.result = {
                    "status": "failed",
                    "summary": f"{tool_name} crashed: {exc}",
                    **record.metadata,
                }
                return record.result
            finally:
                record.finished_at_monotonic = time.monotonic()

        record.task = asyncio.create_task(_runner())
        return await self._wait_or_snapshot(record, wait_sec=wait_sec, poll_tool_name=poll_tool_name)

    async def _wait_or_snapshot(
        self,
        record: JobRecord,
        *,
        wait_sec: float,
        poll_tool_name: str,
    ) -> dict[str, Any]:
        task = record.task
        if task is not None and not task.done():
            if wait_sec > 0:
                await asyncio.wait({task}, timeout=wait_sec)
            else:
                await asyncio.sleep(0)

        elapsed = round(record.elapsed_sec(), 2)
        if task is None or task.done():
            if record.status == "cancelled":
                return {
                    "status": "cancelled",
                    "job_id": record.job_id,
                    "tool": record.tool_name,
                    "elapsed_sec": elapsed,
                    **record.metadata,
                    "summary": f"Job '{record.job_id}' was cancelled.",
                }
            return {
                **(record.result or {}),
                "job_id": record.job_id,
                "elapsed_sec": elapsed,
            }

        return {
            "status": "running",
            "job_id": record.job_id,
            "tool": record.tool_name,
            "elapsed_sec": elapsed,
            **record.metadata,
            "summary": (
                f"{record.tool_name} is still running in background "
                f"(elapsed {record.elapsed_sec():.1f}s). Call {poll_tool_name}("
                f"job_id='{record.job_id}', wait_sec=120) to wait for completion "
                "without hitting the 3-minute MCP RPC timeout."
            ),
        }

    async def poll(
        self,
        *,
        job_id: str | None,
        wait_sec: float,
        cancel: bool,
        poll_tool_name: str,
    ) -> dict[str, Any]:
        """List recent jobs, wait on a running job_id, or cancel it and kill its subprocess."""
        if not job_id or not job_id.strip():
            items = [
                {
                    "job_id": rec.job_id,
                    "tool": rec.tool_name,
                    "status": rec.status,
                    "elapsed_sec": round(rec.elapsed_sec(), 2),
                    "created_at": rec.created_at_iso,
                    "metadata": dict(rec.metadata),
                }
                for rec in reversed(list(self._jobs.values()))
            ]
            return {"status": "success", "count": len(items), "jobs": items}

        clean_id = job_id.strip()
        record = self._jobs.get(clean_id)
        if record is None:
            return {
                "status": "failed",
                "job_id": clean_id,
                "summary": f"Unknown job_id '{clean_id}'.",
            }

        if cancel:
            if record.task is not None and not record.task.done():
                record.task.cancel()
                await asyncio.wait({record.task}, timeout=5.0)
            return {
                "status": "cancelled",
                "job_id": record.job_id,
                "tool": record.tool_name,
                "elapsed_sec": round(record.elapsed_sec(), 2),
                **record.metadata,
                "summary": f"Job '{record.job_id}' was cancelled and its subprocess was terminated.",
            }

        return await self._wait_or_snapshot(record, wait_sec=wait_sec, poll_tool_name=poll_tool_name)
