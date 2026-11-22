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


class _FakeAppServerStdin:
    def __init__(self, on_message: Any) -> None:
        self._buffer = bytearray()
        self._on_message = on_message
        self.messages: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            raw_line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            msg = json.loads(line)
            self.messages.append(msg)
            self._on_message(msg)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeAppServerProcess:
    def __init__(
        self,
        responder: Any,
        *,
        stderr_bytes: bytes = b"",
    ) -> None:
        self.pid = 52525
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader(limit=16 * 1024 * 1024)
        self.stderr = asyncio.StreamReader(limit=1024 * 1024)
        if stderr_bytes:
            self.stderr.feed_data(stderr_bytes)
        self.stderr.feed_eof()
        self.stdin = _FakeAppServerStdin(lambda msg: responder(msg, self))
        self.terminated = False
        self.killed = False

    def feed_json(self, payload: dict[str, Any]) -> None:
        self.stdout.feed_data((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


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
    cfg = CodexConfig(allowed_root=tmp_path, windows_sandbox="elevated", backend="exec")
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
    assert run_res["backend"] == "exec"
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
    assert reply_res["backend"] == "exec"
    assert reply_res["thread_id"] == "thread-abc"
    assert reply_res["model"] == "gpt-5.6-luna"
    assert reply_res["summary"] == "Resumed OK"
    assert calls[1]["cmd"][1:5] == ["exec", "resume", "-m", "gpt-5.6-luna"]
    assert "approval_policy=never" in calls[1]["cmd"]
    assert "windows.sandbox=elevated" in calls[1]["cmd"]
    assert calls[1]["cmd"][-2:] == ["thread-abc", "-"]


@pytest.mark.asyncio
async def test_codex_worktree_falls_back_to_exec_when_backend_is_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = CodexConfig(allowed_root=tmp_path, backend="app-server", windows_sandbox="elevated")
    adapter = CodexExecAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: "/fake/codex")

    seen_cmds: list[list[str]] = []

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeCodexProcess:
        seen_cmds.append(list(cmd))
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-wt"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Worktree OK"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 4}}),
            ]
        ).encode("utf-8")
        return _FakeCodexProcess(returncode=0, stdout=stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_session(
        prompt="Worktree task",
        cwd=str(tmp_path),
        sandbox="workspace-write",
        worktree=True,
    )
    assert res["status"] == "success"
    assert res["backend"] == "exec"
    assert res["thread_id"] == "thread-wt"
    assert seen_cmds[0][1] == "exec"
    assert "--worktree" in seen_cmds[0]


@pytest.mark.asyncio
async def test_codex_app_server_run_and_reply_creates_vscode_thread_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = CodexConfig(allowed_root=tmp_path, backend="app-server", windows_sandbox="elevated")
    adapter = CodexExecAdapter(cfg)
    monkeypatch.setattr(adapter, "find_executable", lambda: r"C:\Program Files\WindowsApps\OpenAI.Codex\codex.exe")

    spawned: list[_FakeAppServerProcess] = []
    spawn_cmds: list[list[str]] = []

    def responder(msg: dict[str, Any], proc: _FakeAppServerProcess) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"userAgent": "multi-agent-orchestrator/0.171"}})
        elif method == "thread/start":
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "thread": {
                            "id": "thread-app-001",
                            "source": "vscode",
                            "cwd": str(tmp_path),
                        },
                        "model": "gpt-6-luna",
                    },
                }
            )
        elif method == "thread/resume":
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "thread": {
                            "id": msg["params"]["threadId"],
                            "source": "vscode",
                            "cwd": str(tmp_path),
                        }
                    },
                }
            )
        elif method == "turn/start":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
            if msg["params"]["threadId"] == "thread-app-001" and msg["params"]["input"][0]["text"] == "Create file":
                (tmp_path / "app_out.py").write_text("x = 1\n", encoding="utf-8")
                text = "Created app_out.py"
            else:
                text = "Follow-up complete"
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": msg["params"]["threadId"],
                        "turnId": "turn-1",
                        "item": {"id": "item-1", "type": "agentMessage", "text": text},
                    },
                }
            )
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": msg["params"]["threadId"],
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 200,
                                "cachedInputTokens": 120,
                                "outputTokens": 15,
                                "reasoningOutputTokens": 0,
                                "totalTokens": 215,
                            }
                        },
                    },
                }
            )
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": msg["params"]["threadId"],
                        "turn": {"id": "turn-1", "status": "completed", "error": None},
                    },
                }
            )

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeAppServerProcess:
        spawn_cmds.append(list(cmd))
        proc = _FakeAppServerProcess(responder)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    run_res = await adapter.run_session(
        prompt="Create file",
        cwd=str(tmp_path),
        sandbox="workspace-write",
    )
    assert run_res["status"] == "success"
    assert run_res["backend"] == "app-server"
    assert run_res["source"] == "vscode"
    assert run_res["thread_id"] == "thread-app-001"
    assert run_res["model"] == "gpt-6-luna"
    assert run_res["summary"] == "Created app_out.py"
    assert run_res["usage"] == {
        "input_tokens": 200,
        "cached_input_tokens": 120,
        "output_tokens": 15,
        "reasoning_output_tokens": 0,
        "total_tokens": 215,
    }
    assert str((tmp_path / "app_out.py").resolve()) in run_res["artifacts"]
    assert spawn_cmds[0][1:4] == ["app-server", "--listen", "stdio://"]

    start_msg = next(m for m in spawned[0].stdin.messages if m.get("method") == "thread/start")
    assert start_msg["params"]["ephemeral"] is False
    assert start_msg["params"]["model"] == "gpt-6-luna"
    assert start_msg["params"]["sandbox"] == "workspace-write"

    reply_res = await adapter.reply_session(
        thread_id="thread-app-001",
        prompt="Explain it",
        cwd=str(tmp_path),
        model="gpt-5.6-luna",
        sandbox="read-only",
    )
    assert reply_res["status"] == "success"
    assert reply_res["backend"] == "app-server"
    assert reply_res["source"] == "vscode"
    assert reply_res["thread_id"] == "thread-app-001"
    assert reply_res["model"] == "gpt-5.6-luna"
    assert reply_res["summary"] == "Follow-up complete"

    resume_msg = next(m for m in spawned[1].stdin.messages if m.get("method") == "thread/resume")
    assert resume_msg["params"]["threadId"] == "thread-app-001"
    assert resume_msg["params"]["sandbox"] == "read-only"
    turn_msg = next(m for m in spawned[1].stdin.messages if m.get("method") == "turn/start")
    assert turn_msg["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}


@pytest.mark.asyncio
async def test_codex_app_server_rejects_server_requests_without_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexExecAdapter(CodexConfig(allowed_root=tmp_path, backend="app-server"))
    monkeypatch.setattr(adapter, "find_executable", lambda: "/fake/codex")

    captured: list[_FakeAppServerProcess] = []

    def responder(msg: dict[str, Any], proc: _FakeAppServerProcess) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "thread/start":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"thread": {"id": "t-srvreq", "source": "vscode"}}})
        elif method == "turn/start":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"turn": {"id": "turn-1"}}})
            # Server sends an interactive approval request mid-turn!
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "echo hi"},
                }
            )
        elif req_id == 99 and "error" in msg:
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {"item": {"id": "m1", "type": "agentMessage", "text": "Recovered cleanly"}},
                }
            )
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed", "error": None}},
                }
            )

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeAppServerProcess:
        proc = _FakeAppServerProcess(responder)
        captured.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_session(prompt="test approval guard", cwd=str(tmp_path), sandbox="read-only")
    assert res["status"] == "success"
    assert res["summary"] == "Recovered cleanly"
    err_reply = next(m for m in captured[0].stdin.messages if m.get("id") == 99)
    assert err_reply["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_codex_app_server_timeout_kills_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexExecAdapter(CodexConfig(allowed_root=tmp_path, backend="app-server"))
    monkeypatch.setattr(adapter, "find_executable", lambda: "/fake/codex")

    hanging_proc: list[_FakeAppServerProcess] = []

    def responder(msg: dict[str, Any], proc: _FakeAppServerProcess) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "thread/start":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"thread": {"id": "t-hang", "source": "vscode"}}})
        elif method == "turn/start":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {"turn": {"id": "turn-1"}}})
            # Intentionally never emit turn/completed.

    async def fake_exec(*cmd: str, **kwargs: Any) -> Any:
        if cmd[0] == "taskkill":
            return _FakeCodexProcess(returncode=0)
        proc = _FakeAppServerProcess(responder)
        hanging_proc.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await adapter.run_session(
        prompt="hang app server",
        cwd=str(tmp_path),
        timeout_sec=0.05,  # type: ignore[arg-type]
    )
    assert res["status"] == "timeout"
    assert res["backend"] == "app-server"
    assert hanging_proc[0].killed is True


@pytest.mark.asyncio
async def test_codex_list_threads_and_read_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexExecAdapter(CodexConfig(allowed_root=tmp_path, backend="app-server"))
    monkeypatch.setattr(adapter, "find_executable", lambda: "/fake/codex")

    def responder(msg: dict[str, Any], proc: _FakeAppServerProcess) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            proc.feed_json({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "thread/list":
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "data": [
                            {
                                "id": "t-1",
                                "name": "Demo Thread",
                                "preview": "Inspect repo",
                                "cwd": str(tmp_path),
                                "source": "vscode",
                                "originator": "multi-agent-orchestrator",
                                "model": "gpt-6-luna",
                                "createdAt": 1000,
                                "updatedAt": 2000,
                                "path": "/rollout.jsonl",
                            }
                        ]
                    },
                }
            )
        elif method == "thread/read":
            proc.feed_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "thread": {
                            "id": "t-1",
                            "preview": "Inspect repo",
                            "cwd": str(tmp_path),
                            "source": "vscode",
                            "turns": [
                                {
                                    "id": "turn-1",
                                    "status": "completed",
                                    "items": [
                                        {
                                            "type": "userMessage",
                                            "id": "u1",
                                            "content": [{"type": "text", "text": "Inspect repo"}],
                                        },
                                        {
                                            "type": "commandExecution",
                                            "id": "c1",
                                            "command": "git status",
                                            "status": "completed",
                                            "exitCode": 0,
                                            "aggregatedOutput": "very long noise",
                                        },
                                        {
                                            "type": "agentMessage",
                                            "id": "a1",
                                            "text": "Clean working tree.",
                                        },
                                    ],
                                }
                            ],
                        }
                    },
                }
            )

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeAppServerProcess:
        return _FakeAppServerProcess(responder)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    listed = await adapter.list_threads(cwd=str(tmp_path), limit=10)
    assert listed["status"] == "success"
    assert listed["count"] == 1
    assert listed["threads"][0]["thread_id"] == "t-1"
    assert listed["threads"][0]["source"] == "vscode"

    read = await adapter.read_thread(thread_id="t-1", max_turns=5)
    assert read["status"] == "success"
    assert read["total_turns"] == 1
    assert read["returned_turns"] == 1
    items = read["turns"][0]["items"]
    assert [it["type"] for it in items] == ["userMessage", "commandExecution", "agentMessage"]
    assert "aggregatedOutput" not in items[1]
    assert items[2]["text"] == "Clean working tree."


@pytest.mark.asyncio
async def test_codex_timeout_kills_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexExecAdapter(CodexConfig(allowed_root=tmp_path, backend="exec"))
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
    assert res["backend"] == "exec"
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
    assert {t.name for t in tools} == {
        "codex",
        "codex-reply",
        "codex_health_check",
        "codex_job_status",
        "codex_list_threads",
        "codex_read_thread",
    }

    health = _extract_tool_dict(await server.call_tool("codex_health_check", {}))
    assert health["ready"] is True
    assert health["backend"] == "app-server"
    assert health["max_sync_wait_sec"] == 120
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


@pytest.mark.asyncio
async def test_codex_server_async_detach_poll_and_cancel(tmp_path: Path) -> None:
    cfg = CodexConfig(allowed_root=tmp_path)
    adapter = CodexExecAdapter(cfg)
    gate = asyncio.Event()
    cancel_gate = asyncio.Event()
    cancelled_inside = asyncio.Event()

    async def fake_run_session(**kwargs: Any) -> dict[str, Any]:
        cb = kwargs.get("on_thread_id")
        if cb is not None:
            cb("thread-live-999")
        prompt = kwargs["prompt"]
        if prompt == "hang forever":
            try:
                await cancel_gate.wait()
            except asyncio.CancelledError:
                cancelled_inside.set()
                raise
        await gate.wait()
        return {
            "status": "success",
            "thread_id": "thread-live-999",
            "model": "gpt-6-luna",
            "summary": "Async complete",
            "artifacts": [],
            "usage": None,
            "logs_tail": "",
            "exit_code": 0,
            "backend": "app-server",
            "source": "vscode",
        }

    adapter.run_session = fake_run_session  # type: ignore[method-assign]
    server = create_codex_server(cfg, adapter=adapter)

    running = _extract_tool_dict(
        await server.call_tool(
            "codex",
            {"prompt": "slow step", "cwd": str(tmp_path), "wait_sec": 0},
        )
    )
    assert running["status"] == "running"
    assert running["thread_id"] == "thread-live-999"
    job_id = running["job_id"]

    gate.set()
    completed = _extract_tool_dict(
        await server.call_tool(
            "codex_job_status",
            {"job_id": job_id, "wait_sec": 2},
        )
    )
    assert completed["status"] == "success"
    assert completed["thread_id"] == "thread-live-999"
    assert completed["summary"] == "Async complete"

    hang_res = _extract_tool_dict(
        await server.call_tool(
            "codex",
            {"prompt": "hang forever", "cwd": str(tmp_path), "wait_sec": 0},
        )
    )
    assert hang_res["status"] == "running"

    cancel_res = _extract_tool_dict(
        await server.call_tool(
            "codex_job_status",
            {"job_id": hang_res["job_id"], "cancel": True},
        )
    )
    assert cancel_res["status"] == "cancelled"
    assert cancelled_inside.is_set()
