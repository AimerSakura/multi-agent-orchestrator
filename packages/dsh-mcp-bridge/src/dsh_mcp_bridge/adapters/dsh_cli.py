"""DeepSeek Harness (dsh) CLI subprocess adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dsh_mcp_bridge.config import BridgeConfig
from dsh_mcp_bridge.logger import get_logger, truncate_tail
from dsh_mcp_bridge.security import diff_artifacts, snapshot_workspace

from .base import ResumeTaskRequest, RunTaskRequest, TaskResult

_LOGGER = get_logger("dsh_mcp_bridge.dsh_cli")
_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
)


@dataclass(slots=True)
class ParsedDshOutput:
    """Structured data extracted from `dsh --profile headless --json` NDJSON output."""

    session_id: str = ""
    final_text: str = ""
    last_text: str = ""
    errors: list[str] = field(default_factory=list)
    tool_events: list[str] = field(default_factory=list)
    noise_lines: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def summary_text(self) -> str:
        """Return final answer text, falling back to last committed text block or error."""
        if self.final_text.strip():
            return self.final_text.strip()
        if self.last_text.strip():
            return self.last_text.strip()
        if self.errors:
            return "; ".join(self.errors)
        return ""


def parse_dsh_json_stream(stdout_text: str) -> ParsedDshOutput:
    """Parse newline-delimited JSON events emitted by `dsh --profile headless --json`."""
    parsed = ParsedDshOutput()

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.noise_lines.append(line)
            continue

        if not isinstance(event, dict):
            parsed.noise_lines.append(line)
            continue

        event_type = event.get("type")
        if event_type == "session":
            session_id = event.get("sessionId")
            if isinstance(session_id, str) and session_id:
                parsed.session_id = session_id
        elif event_type == "final":
            text = event.get("text")
            if isinstance(text, str):
                parsed.final_text = text
        elif event_type == "text":
            text = event.get("text")
            if isinstance(text, str) and text:
                parsed.last_text = text
        elif event_type == "error":
            message = event.get("message")
            if isinstance(message, str) and message:
                parsed.errors.append(message)
        elif event_type == "tool_call":
            tool_name = event.get("tool", "unknown")
            parsed.tool_events.append(f"tool_call: {tool_name}")
        elif event_type == "tool_result":
            status = event.get("status", "unknown")
            parsed.tool_events.append(f"tool_result: {status}")
        elif event_type == "status" and event.get("phase") == "step_end":
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in _USAGE_KEYS:
                    val = usage.get(key)
                    if isinstance(val, int):
                        parsed.usage[key] = parsed.usage.get(key, 0) + val

    return parsed


def build_patch_yaml(model_name: str, approval_policy: str) -> str:
    """Generate a Cordis patch-list YAML selecting the model and approval policy."""
    entries = [
        {
            "id": "agent-default-model",
            "config": {
                "provider": "deepseek-official",
                "model": model_name,
            },
        },
        {
            "id": "approval",
            "config": {
                "policy": approval_policy,
            },
        },
    ]
    return yaml.safe_dump(entries, sort_keys=False, allow_unicode=True)


def wrap_command_for_platform(cmd: list[str]) -> list[str]:
    """Wrap Windows .cmd/.bat shims with cmd.exe so asyncio subprocesses execute reliably."""
    if os.name == "nt" and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/c", *cmd]
    return cmd


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its descendant tree cross-platform."""
    if proc.returncode is not None:
        return

    if os.name == "nt" and proc.pid:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except Exception as exc:
            _LOGGER.debug("taskkill fallback notice for PID %s: %s", proc.pid, exc)

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        _LOGGER.warning("Timed out waiting for terminated process PID %s", proc.pid)


class DshCliAdapter:
    """Adapter executing tasks via local `dsh --profile headless` subprocesses."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def find_executable(self) -> str | None:
        """Locate the configured `dsh` executable on PATH or absolute path."""
        return shutil.which(self.config.dsh_bin)

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config.dsh_home:
            env["DSH_HOME"] = self.config.dsh_home
        return env

    def _build_command(
        self,
        executable: str,
        patch_path: Path,
        session_id: str | None = None,
    ) -> list[str]:
        cmd = [
            executable,
            "--profile",
            "headless",
            "--patch",
            str(patch_path),
            "--json",
        ]
        if session_id:
            cmd.extend(["--session-id", session_id])
        cmd.append("-")
        return wrap_command_for_platform(cmd)

    async def check_health(self) -> dict[str, Any]:
        """Probe whether `dsh` CLI exists and responds to `--version`."""
        executable = self.find_executable()
        if not executable:
            return {
                "available": False,
                "executable": None,
                "version": None,
                "error": f"Executable '{self.config.dsh_bin}' not found in PATH.",
            }

        cmd = wrap_command_for_platform([executable, "--version"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                return {
                    "available": True,
                    "executable": executable,
                    "version": stdout or "unknown",
                    "error": None,
                }
            return {
                "available": False,
                "executable": executable,
                "version": None,
                "error": stderr or stdout or f"Exited with code {proc.returncode}",
            }
        except Exception as exc:
            return {
                "available": False,
                "executable": executable,
                "version": None,
                "error": str(exc),
            }

    async def run_task(self, request: RunTaskRequest) -> TaskResult:
        """Run a fresh task via `dsh --profile headless --json`."""
        prompt = request.task_prompt.strip()
        if request.output_file is not None:
            prompt = (
                f"{prompt}\n\n"
                f"[Bridge Output Instruction]\n"
                f"Write the primary deliverable or structured summary to: {request.output_file}"
            )

        return await self._execute_headless(
            prompt=prompt,
            workspace_dir=request.workspace_dir,
            model_tier=request.model_tier,
            timeout_sec=request.timeout_sec,
            output_file=request.output_file,
            session_id=None,
            parent_trajectory_id=None,
        )

    async def resume_or_fork(self, request: ResumeTaskRequest) -> TaskResult:
        """Resume an existing `dsh` session or start a forked session seeded with parent context."""
        if request.mode == "resume":
            prompt = request.new_instruction.strip() or (
                "Continue from the previous trajectory state and provide the updated result."
            )
            return await self._execute_headless(
                prompt=prompt,
                workspace_dir=request.workspace_dir,
                model_tier=request.model_tier,
                timeout_sec=request.timeout_sec,
                output_file=None,
                session_id=request.trajectory_id,
                parent_trajectory_id=None,
            )

        fork_prompt = self._build_fork_prompt(request)
        return await self._execute_headless(
            prompt=fork_prompt,
            workspace_dir=request.workspace_dir,
            model_tier=request.model_tier,
            timeout_sec=request.timeout_sec,
            output_file=None,
            session_id=None,
            parent_trajectory_id=request.trajectory_id,
        )

    def _build_fork_prompt(self, request: ResumeTaskRequest) -> str:
        sections = [
            f"[Forked Trajectory Context from {request.trajectory_id}]",
        ]
        if request.parent_record is not None:
            if request.parent_record.summary:
                sections.append(f"Prior summary:\n{request.parent_record.summary}")
            if request.parent_record.artifacts:
                artifacts_str = "\n".join(f"- {item}" for item in request.parent_record.artifacts)
                sections.append(f"Prior artifacts:\n{artifacts_str}")

        session_excerpt = self._read_session_excerpt(request.trajectory_id)
        if session_excerpt:
            sections.append(f"Recent session log excerpt:\n{session_excerpt}")

        instruction = request.new_instruction.strip() or (
            "Re-evaluate the prior trajectory above and complete the remaining work cleanly."
        )
        sections.append(f"[New Instruction]\n{instruction}")
        return "\n\n".join(sections)

    def _read_session_excerpt(self, trajectory_id: str, max_chars: int = 2000) -> str:
        if not self.config.dsh_home:
            return ""
        session_file = Path(self.config.dsh_home) / "sessions" / f"{trajectory_id}.jsonl"
        if not session_file.is_file():
            return ""
        try:
            content = session_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return truncate_tail(content.strip(), max_chars)

    async def _execute_headless(
        self,
        *,
        prompt: str,
        workspace_dir: Path,
        model_tier: str,
        timeout_sec: int,
        output_file: Path | None,
        session_id: str | None,
        parent_trajectory_id: str | None,
    ) -> TaskResult:
        executable = self.find_executable()
        if not executable:
            return TaskResult(
                status="failed",
                trajectory_id=session_id or "",
                summary=f"dsh executable '{self.config.dsh_bin}' was not found in PATH.",
                artifacts=[],
                logs_tail="",
                adapter="dsh-cli",
                parent_trajectory_id=parent_trajectory_id,
            )

        model_name = self.config.resolve_model_name(model_tier)
        patch_content = build_patch_yaml(model_name, self.config.approval_policy)
        before_snapshot = snapshot_workspace(workspace_dir)

        patch_file = tempfile.NamedTemporaryFile(
            mode="w",
            prefix="dsh-patch-",
            suffix=".yml",
            encoding="utf-8",
            delete=False,
        )
        patch_path = Path(patch_file.name)
        try:
            patch_file.write(patch_content)
            patch_file.close()

            cmd = self._build_command(executable, patch_path, session_id=session_id)
            env = self._build_env()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=float(timeout_sec),
                )
            except asyncio.TimeoutError:
                await kill_process_tree(proc)
                artifacts = diff_artifacts(workspace_dir, before_snapshot, output_file=output_file)
                return TaskResult(
                    status="timeout",
                    trajectory_id=session_id or "",
                    summary=f"dsh execution timed out after {timeout_sec}s and the process tree was terminated.",
                    artifacts=artifacts,
                    logs_tail=f"Process killed after timeout ({timeout_sec}s).",
                    adapter="dsh-cli",
                    exit_code=proc.returncode,
                    parent_trajectory_id=parent_trajectory_id,
                )

            stdout_text = stdout_b.decode("utf-8", errors="replace")
            stderr_text = stderr_b.decode("utf-8", errors="replace")
            parsed = parse_dsh_json_stream(stdout_text)
            artifacts = diff_artifacts(workspace_dir, before_snapshot, output_file=output_file)

            effective_trajectory = parsed.session_id or session_id or ""
            succeeded = proc.returncode == 0 and not parsed.errors
            status = "success" if succeeded else "failed"

            summary = parsed.summary_text
            if not summary:
                summary = (
                    stderr_text.strip()
                    or f"dsh exited with status code {proc.returncode}."
                )

            logs_parts: list[str] = []
            if parsed.tool_events:
                logs_parts.append("\n".join(parsed.tool_events))
            if parsed.noise_lines:
                logs_parts.append("\n".join(parsed.noise_lines))
            if stderr_text.strip():
                logs_parts.append(stderr_text.strip())
            logs_tail = truncate_tail("\n".join(logs_parts), self.config.max_log_chars)

            return TaskResult(
                status=status,
                trajectory_id=effective_trajectory,
                summary=summary,
                artifacts=artifacts,
                logs_tail=logs_tail,
                adapter="dsh-cli",
                exit_code=proc.returncode,
                parent_trajectory_id=parent_trajectory_id,
                usage=parsed.usage or None,
            )
        finally:
            if not patch_file.closed:
                patch_file.close()
            try:
                patch_path.unlink(missing_ok=True)
            except OSError:
                pass
