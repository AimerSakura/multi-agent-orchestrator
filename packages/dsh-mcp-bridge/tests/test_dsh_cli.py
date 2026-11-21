"""Tests for dsh CLI adapter, NDJSON stream parsing, patch YAML, and timeouts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from dsh_mcp_bridge.adapters.base import ResumeTaskRequest, RunTaskRequest
from dsh_mcp_bridge.adapters.dsh_cli import (
    DshCliAdapter,
    build_patch_yaml,
    parse_dsh_json_stream,
)
from dsh_mcp_bridge.config import BridgeConfig
from dsh_mcp_bridge.logger import TrajectoryRecord


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ) -> None:
        self.pid = 42424
        self.returncode: int | None = None if hang else returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.stdin_received = b""
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if input is not None:
            self.stdin_received = input
        if self._hang:
            await asyncio.sleep(30)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else -9


def test_parse_dsh_json_stream_complete_and_usage() -> None:
    stream = "\n".join(
        [
            "booting dsh noise line",
            json.dumps({"type": "session", "sessionId": "session-abc", "cwd": "/repo"}),
            json.dumps({"type": "status", "phase": "turn_start", "turn": 1}),
            json.dumps({"type": "tool_call", "callId": "1", "tool": "fs_write", "input": {}}),
            json.dumps({"type": "tool_result", "callId": "1", "status": "completed", "result": "ok"}),
            json.dumps(
                {
                    "type": "status",
                    "phase": "step_end",
                    "turn": 1,
                    "step": 1,
                    "usage": {"inputTokens": 100, "outputTokens": 25, "totalTokens": 125},
                }
            ),
            json.dumps(
                {
                    "type": "status",
                    "phase": "step_end",
                    "turn": 1,
                    "step": 2,
                    "usage": {"inputTokens": 50, "outputTokens": 10, "totalTokens": 60},
                }
            ),
            json.dumps({"type": "final", "text": "All cleaned up."}),
        ]
    )

    parsed = parse_dsh_json_stream(stream)
    assert parsed.session_id == "session-abc"
    assert parsed.summary_text == "All cleaned up."
    assert parsed.errors == []
    assert parsed.noise_lines == ["booting dsh noise line"]
    assert parsed.tool_events == ["tool_call: fs_write", "tool_result: completed"]
    assert parsed.usage == {"inputTokens": 150, "outputTokens": 35, "totalTokens": 185}


def test_parse_dsh_json_stream_falls_back_to_text_and_captures_error() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "session", "sessionId": "session-xyz"}),
            json.dumps({"type": "text", "text": "partial answer"}),
            json.dumps({"type": "error", "message": "model quota failed"}),
        ]
    )

    parsed = parse_dsh_json_stream(stream)
    assert parsed.session_id == "session-xyz"
    assert parsed.summary_text == "partial answer"
    assert parsed.errors == ["model quota failed"]


def test_build_patch_yaml_contains_expected_entries() -> None:
    raw_yaml = build_patch_yaml("deepseek-v4-pro", "never")
    parsed = yaml.safe_load(raw_yaml)
    assert parsed == [
        {
            "id": "agent-default-model",
            "config": {"provider": "deepseek-official", "model": "deepseek-v4-pro"},
        },
        {
            "id": "approval",
            "config": {"policy": "never"},
        },
    ]


@pytest.mark.asyncio
async def test_dsh_cli_adapter_run_task_captures_session_patch_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = BridgeConfig(allowed_root=tmp_path, dsh_home=str(tmp_path / ".dsh-home"))
    adapter = DshCliAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: "/usr/local/bin/dsh")

    captured: dict[str, Any] = {}
    out_file = tmp_path / ".agent-handoff" / "result.md"

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]

        patch_idx = captured["cmd"].index("--patch") + 1
        patch_path = Path(captured["cmd"][patch_idx])
        captured["patch_yaml"] = yaml.safe_load(patch_path.read_text(encoding="utf-8"))

        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("# Deliverable\nDone", encoding="utf-8")

        ndjson = "\n".join(
            [
                json.dumps({"type": "session", "sessionId": "sess-123"}),
                json.dumps({"type": "final", "text": "Task succeeded"}),
            ]
        ).encode("utf-8")
        proc = _FakeProcess(returncode=0, stdout=ndjson)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_task(
        RunTaskRequest(
            task_prompt="Clean data",
            workspace_dir=tmp_path,
            output_file=out_file,
            model_tier="reasoner",
            timeout_sec=30,
        )
    )

    assert res.status == "success"
    assert res.trajectory_id == "sess-123"
    assert res.summary == "Task succeeded"
    assert str(out_file.resolve()) in res.artifacts
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["DSH_HOME"] == str(tmp_path / ".dsh-home")
    assert "--profile" in captured["cmd"] and "headless" in captured["cmd"]
    assert "--json" in captured["cmd"]
    assert captured["cmd"][-1] == "-"
    assert captured["patch_yaml"][0]["config"]["model"] == "deepseek-v4-pro"
    assert str(out_file) in captured["proc"].stdin_received.decode("utf-8")


@pytest.mark.asyncio
async def test_dsh_cli_adapter_timeout_kills_process_and_returns_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = BridgeConfig(allowed_root=tmp_path)
    adapter = DshCliAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: "/usr/local/bin/dsh")

    hanging_proc = _FakeProcess(hang=True)

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        if cmd[0] == "taskkill":
            return _FakeProcess(returncode=0)
        return hanging_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_task(
        RunTaskRequest(
            task_prompt="slow task",
            workspace_dir=tmp_path,
            timeout_sec=0.05,  # type: ignore[arg-type]
        )
    )

    assert res.status == "timeout"
    assert hanging_proc.killed is True


@pytest.mark.asyncio
async def test_dsh_cli_adapter_resume_and_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = BridgeConfig(allowed_root=tmp_path)
    adapter = DshCliAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: "/usr/local/bin/dsh")

    calls: list[dict[str, Any]] = []

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        cmd_list = list(cmd)
        session_id = "sess-new-fork" if "--session-id" not in cmd_list else "sess-orig"
        ndjson = "\n".join(
            [
                json.dumps({"type": "session", "sessionId": session_id}),
                json.dumps({"type": "final", "text": f"done:{session_id}"}),
            ]
        ).encode("utf-8")
        proc = _FakeProcess(returncode=0, stdout=ndjson)
        calls.append({"cmd": cmd_list, "proc": proc})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    resumed = await adapter.resume_or_fork(
        ResumeTaskRequest(
            trajectory_id="sess-orig",
            mode="resume",
            new_instruction="Fix step 2",
            workspace_dir=tmp_path,
            model_tier="default",
            timeout_sec=30,
        )
    )
    assert resumed.status == "success"
    assert resumed.trajectory_id == "sess-orig"
    assert "--session-id" in calls[0]["cmd"]
    assert "sess-orig" in calls[0]["cmd"]

    parent = TrajectoryRecord(
        trajectory_id="sess-orig",
        workspace_dir=str(tmp_path),
        adapter="dsh-cli",
        model_tier="flash",
        status="success",
        summary="Prior summary text",
        artifacts=[str(tmp_path / "draft.md")],
    )
    forked = await adapter.resume_or_fork(
        ResumeTaskRequest(
            trajectory_id="sess-orig",
            mode="fork",
            new_instruction="Try alternative branch",
            workspace_dir=tmp_path,
            model_tier="reasoner",
            timeout_sec=30,
            parent_record=parent,
        )
    )
    assert forked.status == "success"
    assert forked.trajectory_id == "sess-new-fork"
    assert forked.parent_trajectory_id == "sess-orig"
    assert "--session-id" not in calls[1]["cmd"]
    stdin_text = calls[1]["proc"].stdin_received.decode("utf-8")
    assert "Prior summary text" in stdin_text
    assert "Try alternative branch" in stdin_text
