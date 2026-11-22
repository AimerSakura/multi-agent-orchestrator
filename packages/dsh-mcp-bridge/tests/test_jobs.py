"""Unit tests for bounded-wait AsyncJobManager detachment, polling, and cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dsh_mcp_bridge.jobs import AsyncJobManager, JobRecord, clamp_sync_wait


def test_clamp_sync_wait_enforces_bounds() -> None:
    assert clamp_sync_wait(None, default_wait=120) == 120.0
    assert clamp_sync_wait(None, default_wait=200) == 150.0
    assert clamp_sync_wait(-5) == 0.0
    assert clamp_sync_wait(0) == 0.0
    assert clamp_sync_wait(0.02) == 0.02
    assert clamp_sync_wait(180) == 150.0


@pytest.mark.asyncio
async def test_fast_job_completes_synchronously() -> None:
    mgr = AsyncJobManager()

    async def fast_task(_rec: JobRecord) -> dict[str, Any]:
        return {"status": "success", "summary": "done quickly"}

    res = await mgr.start_and_wait(
        tool_name="codex",
        poll_tool_name="codex_job_status",
        coro_factory=fast_task,
        metadata={"cwd": "/repo"},
        wait_sec=2.0,
    )
    assert res["status"] == "success"
    assert res["summary"] == "done quickly"
    assert res["job_id"].startswith("job-")
    assert isinstance(res["elapsed_sec"], float)


@pytest.mark.asyncio
async def test_slow_job_detaches_without_cancellation_and_finishes_on_poll() -> None:
    mgr = AsyncJobManager()
    gate = asyncio.Event()
    cancelled = False

    async def slow_task(rec: JobRecord) -> dict[str, Any]:
        nonlocal cancelled
        rec.metadata["thread_id"] = "t-live-123"
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return {"status": "success", "thread_id": "t-live-123", "summary": "finished"}

    initial = await mgr.start_and_wait(
        tool_name="codex",
        poll_tool_name="codex_job_status",
        coro_factory=slow_task,
        metadata={"cwd": "/repo"},
        wait_sec=0.02,
    )
    assert initial["status"] == "running"
    assert initial["thread_id"] == "t-live-123"
    assert cancelled is False
    job_id = initial["job_id"]

    listed = await mgr.poll(job_id=None, wait_sec=0.0, cancel=False, poll_tool_name="codex_job_status")
    assert listed["status"] == "success"
    assert listed["count"] == 1
    assert listed["jobs"][0]["job_id"] == job_id
    assert listed["jobs"][0]["status"] == "running"

    gate.set()
    final = await mgr.poll(job_id=job_id, wait_sec=2.0, cancel=False, poll_tool_name="codex_job_status")
    assert final["status"] == "success"
    assert final["thread_id"] == "t-live-123"
    assert final["summary"] == "finished"


@pytest.mark.asyncio
async def test_poll_cancel_terminates_running_job_and_triggers_cleanup() -> None:
    mgr = AsyncJobManager()
    cleanup_ran = asyncio.Event()

    async def hanging_task(_rec: JobRecord) -> dict[str, Any]:
        try:
            await asyncio.sleep(60)
            return {"status": "success"}
        except asyncio.CancelledError:
            cleanup_ran.set()
            raise

    initial = await mgr.start_and_wait(
        tool_name="dsh_run_task",
        poll_tool_name="dsh_job_status",
        coro_factory=hanging_task,
        metadata={"workspace_dir": "/repo"},
        wait_sec=0.02,
    )
    assert initial["status"] == "running"

    cancelled_res = await mgr.poll(
        job_id=initial["job_id"],
        wait_sec=1.0,
        cancel=True,
        poll_tool_name="dsh_job_status",
    )
    assert cancelled_res["status"] == "cancelled"
    assert cleanup_ran.is_set()
