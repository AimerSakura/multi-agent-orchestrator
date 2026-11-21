"""Tests for codex-mcp-bridge JSONL parsing, quota guardrails, subprocess execution, and MCPServer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from dsh_mcp_bridge.codex_bridge import CodexConfig, CodexExecAdapter, parse_codex_jsonl
from dsh_mcp_bridge.codex_server import create_codex_server


class _FakeCodexProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ) -> None:
        self.pid = 51515
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


def _extract_tool_dict(call_result: Any) -> dict[str, Any]:
    if isinstance(call_result, tuple):
        for part in call_result:
            if isinstance(part, dict):
                return part
        content_list = call_result[0]
    else:
        structured = getattr(call_result, "structured_content", None)
        if not isinstance(structured, dict):
            structured = getattr(call_result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content_list = getattr(call_result, "content", call_result)

    if isinstance(content_list, dict):
        return content_list
    if isinstance(content_list, list) and content_list:
        text = getattr(content_list[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise AssertionError(f"Could not extract dict from MCPServer tool output: {call_result!r}")


def test_parse_codex_jsonl_extracts_thread_message_usage_and_errors() -> None:
    stream = "\n".join(
        [
            "diagnostic line",
            json.dumps({"type": "thread.started", "thread_id": "thread-001"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "step 1"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "OK"}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 10},
                }
            ),
            json.dumps({"type": "error", "message": "non-fatal warning"}),
        ]
    )

    parsed = parse_codex_jsonl(stream)
    assert parsed.thread_id == "thread-001"
    assert parsed.summary_text == "OK"
    assert parsed.usage == {"input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 10}
    assert parsed.errors == ["non-fatal warning"]
    assert parsed.noise_lines == ["diagnostic line"]


def test_codex_model_quota_allowlist_and_escalation_toggle(tmp_path: Path) -> None:
    locked = CodexExecAdapter(CodexConfig(allowed_root=tmp_path))
    assert locked.validate_model(None) == "gpt-6-luna"
    assert locked.validate_model("gpt-6-luna") == "gpt-6-luna"
    assert locked.validate_model("gpt-5.6-luna") == "gpt-5.6-luna"

    for blocked_model in ("gpt-6-sol", "gpt-6.2-sol", "gpt-5.6-sol"):
        with pytest.raises(ValueError, match="blocked by quota policy"):
            locked.validate_model(blocked_model)

    escalated = CodexExecAdapter(CodexConfig(allowed_root=tmp_path, allow_escalation=True))
    assert escalated.validate_model("gpt-6.2-sol") == "gpt-6.2-sol"


@pytest.mark.asyncio
async def test_codex_run_session_blocks_danger_sandbox_and_outside_cwd(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    adapter = CodexExecAdapter(CodexConfig(allowed_root=allowed))

    bad_sandbox = await adapter.run_session(
        prompt="check",
        cwd=str(allowed),
        sandbox="danger-full-access",
    )
    assert bad_sandbox["status"] == "failed"
    assert "blocked by security policy" in bad_sandbox["summary"]

    bad_cwd = await adapter.run_session(
        prompt="check",
        cwd=str(outside),
        sandbox="read-only",
    )
    assert bad_cwd["status"] == "failed"
    assert "Security validation failed" in bad_cwd["summary"]


@pytest.mark.asyncio
async def test_codex_run_and_reply_executes_expected_flags_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = CodexConfig(allowed_root=tmp_path, windows_sandbox="elevated")
    adapter = CodexExecAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: r"C:\Program Files\WindowsApps\OpenAI.Codex\codex.exe")

    calls: list[dict[str, Any]] = []

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeCodexProcess:
        cmd_list = list(cmd)
        if "resume" in cmd_list:
            stdout = "\n".join(
                [
                    json.dumps({"type": "turn.started"}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Resumed OK"}}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 6}}),
                ]
            ).encode("utf-8")
        else:
            (tmp_path / "generated.py").write_text("print('hi')\n", encoding="utf-8")
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-abc"}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Created file"}}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 12}}),
                ]
            ).encode("utf-8")

        proc = _FakeCodexProcess(returncode=0, stdout=stdout)
        calls.append({"cmd": cmd_list, "cwd": kwargs["cwd"], "proc": proc})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    run_res = await adapter.run_session(
        prompt="Create generated.py",
        cwd=str(tmp_path),
        sandbox="workspace-write",
        worktree=True,
    )
    assert run_res["status"] == "success"
    assert run_res["thread_id"] == "thread-abc"
    assert run_res["model"] == "gpt-6-luna"
    assert run_res["summary"] == "Created file"
    assert str((tmp_path / "generated.py").resolve()) in run_res["artifacts"]
    assert calls[0]["cmd"][1:5] == ["exec", "-m", "gpt-6-luna", "-s"]
    assert "--ignore-user-config" in calls[0]["cmd"]
    assert "--worktree" in calls[0]["cmd"]
    assert "approval_policy=never" in calls[0]["cmd"]
    assert "windows.sandbox=elevated" in calls[0]["cmd"]
    assert calls[0]["cmd"][-1] == "-"
    assert calls[0]["proc"].stdin_received == b"Create generated.py"

    reply_res = await adapter.reply_session(
        thread_id="thread-abc",
        prompt="Now explain it",
        cwd=str(tmp_path),
        model="gpt-5.6-luna",
    )
    assert reply_res["status"] == "success"
    assert reply_res["thread_id"] == "thread-abc"
    assert reply_res["model"] == "gpt-5.6-luna"
    assert reply_res["summary"] == "Resumed OK"
    assert calls[1]["cmd"][1:5] == ["exec", "resume", "-m", "gpt-5.6-luna"]
    assert "approval_policy=never" in calls[1]["cmd"]
    assert "windows.sandbox=elevated" in calls[1]["cmd"]
    assert calls[1]["cmd"][-2:] == ["thread-abc", "-"]


@pytest.mark.asyncio
async def test_codex_timeout_kills_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexExecAdapter(CodexConfig(allowed_root=tmp_path))
    monkeypatch.setattr(adapter, "find_executable", lambda: "/fake/codex")

    hanging = _FakeCodexProcess(hang=True)

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeCodexProcess:
        if cmd[0] == "taskkill":
            return _FakeCodexProcess(returncode=0)
        return hanging

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_session(
        prompt="hang",
        cwd=str(tmp_path),
        timeout_sec=0.05,  # type: ignore[arg-type]
    )
    assert res["status"] == "timeout"
    assert hanging.killed is True


@pytest.mark.asyncio
async def test_create_codex_server_registers_tools_and_enforces_quota(tmp_path: Path) -> None:
    cfg = CodexConfig(allowed_root=tmp_path, windows_sandbox="elevated")
    adapter = CodexExecAdapter(cfg)

    async def fake_health() -> dict[str, Any]:
        return {"available": True, "executable": "/fake/codex.exe", "version": "codex-cli 0.171.0-alpha.13", "error": None}

    adapter.check_health = fake_health  # type: ignore[method-assign]
    server = create_codex_server(cfg, adapter=adapter)

    tools = await server.list_tools()
    assert {t.name for t in tools} == {"codex", "codex-reply", "codex_health_check"}

    health = _extract_tool_dict(await server.call_tool("codex_health_check", {}))
    assert health["ready"] is True
    assert health["default_model"] == "gpt-6-luna"
    assert health["allowed_models"] == ["gpt-6-luna", "gpt-5.6-luna"]
    assert health["allow_escalation"] is False

    blocked = _extract_tool_dict(
        await server.call_tool(
            "codex",
            {
                "prompt": "try flagship",
                "cwd": str(tmp_path),
                "model": "gpt-6.2-sol",
            },
        )
    )
    assert blocked["status"] == "failed"
    assert "blocked by quota policy" in blocked["summary"]
