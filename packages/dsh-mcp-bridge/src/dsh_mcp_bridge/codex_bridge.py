"""Codex CLI (`codex exec --json`) execution adapter and quota-lock configuration."""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from dsh_mcp_bridge.adapters.dsh_cli import kill_process_tree, wrap_command_for_platform
from dsh_mcp_bridge.config import parse_bool, parse_int, resolve_allowed_root
from dsh_mcp_bridge.logger import truncate_tail
from dsh_mcp_bridge.security import (
    PathSecurityError,
    diff_artifacts,
    snapshot_workspace,
    validate_workspace_dir,
)

VALID_CODEX_SANDBOXES = ("read-only", "workspace-write")
VALID_CODEX_APPROVAL_POLICIES = ("never", "on-request")
DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_ALLOWED_CODEX_MODELS = ("gpt-6-luna", "gpt-5.6-luna")


def _read_codex_cli_from_user_config() -> str | None:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    candidate = (
        data.get("mcp_servers", {})
        .get("node_repl", {})
        .get("env", {})
        .get("CODEX_CLI_PATH")
    )
    if isinstance(candidate, str) and candidate.strip():
        resolved = Path(candidate.strip())
        if resolved.is_file():
            return str(resolved)
    return None


def _glob_windowsapps_codex() -> str | None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    pattern = str(Path(program_files) / "WindowsApps" / "OpenAI.Codex_*" / "app" / "resources" / "codex.exe")
    try:
        matches = sorted(glob.glob(pattern), reverse=True)
    except OSError:
        return None
    for match in matches:
        if Path(match).is_file():
            return match
    return None


def discover_codex_executable(configured_bin: str | None = None) -> str | None:
    """Locate the Codex CLI binary, preferring Desktop App's newer Windows binary over PATH."""
    if configured_bin and configured_bin.strip():
        raw = configured_bin.strip()
        candidate = Path(raw)
        if candidate.is_file():
            return str(candidate)
        return shutil.which(raw)

    if os.name == "nt":
        from_toml = _read_codex_cli_from_user_config()
        if from_toml:
            return from_toml
        from_windowsapps = _glob_windowsapps_codex()
        if from_windowsapps:
            return from_windowsapps

    return shutil.which("codex")


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """Runtime settings for codex-mcp-bridge enforcing model quota and path guardrails."""

    codex_bin: str | None = None
    allowed_root: Path = Path(".")
    default_model: str = DEFAULT_CODEX_MODEL
    allowed_models: tuple[str, ...] = DEFAULT_ALLOWED_CODEX_MODELS
    allow_escalation: bool = False
    default_timeout: int = 300
    max_log_chars: int = 4000
    ignore_user_config: bool = True
    approval_policy: str = "never"
    windows_sandbox: str | None = "elevated" if os.name == "nt" else None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CodexConfig:
        """Build CodexConfig from environment variables (defaults to os.environ)."""
        env = os.environ if environ is None else environ

        raw_bin = env.get("CODEX_BIN", "").strip()
        codex_bin = raw_bin if raw_bin else None

        allowed_root = resolve_allowed_root(env, "CODEX_ALLOWED_ROOT", "DSH_ALLOWED_ROOT")
        default_model = env.get("CODEX_DEFAULT_MODEL", DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL

        raw_allowed = env.get("CODEX_ALLOWED_MODELS", "")
        parsed_models = [item.strip() for item in raw_allowed.split(",") if item.strip()]
        if not parsed_models:
            parsed_models = list(DEFAULT_ALLOWED_CODEX_MODELS)
        if default_model not in parsed_models:
            parsed_models.insert(0, default_model)

        raw_approval = env.get("CODEX_APPROVAL_POLICY", "never").strip().lower()
        approval_policy = raw_approval if raw_approval in VALID_CODEX_APPROVAL_POLICIES else "never"

        raw_win_sandbox = env.get("CODEX_WINDOWS_SANDBOX")
        if raw_win_sandbox is None:
            windows_sandbox: str | None = "elevated" if os.name == "nt" else None
        else:
            cleaned = raw_win_sandbox.strip().lower()
            windows_sandbox = None if cleaned in {"", "none", "off"} else cleaned

        return cls(
            codex_bin=codex_bin,
            allowed_root=allowed_root,
            default_model=default_model,
            allowed_models=tuple(dict.fromkeys(parsed_models)),
            allow_escalation=parse_bool(env.get("CODEX_ALLOW_ESCALATION"), default=False),
            default_timeout=parse_int(env.get("CODEX_DEFAULT_TIMEOUT"), 300, 1, 3600),
            max_log_chars=parse_int(env.get("CODEX_MAX_LOG_CHARS"), 4000, 256, 100_000),
            ignore_user_config=parse_bool(env.get("CODEX_IGNORE_USER_CONFIG"), default=True),
            approval_policy=approval_policy,
            windows_sandbox=windows_sandbox,
        )

    def resolve_timeout(self, timeout_sec: int | None) -> int:
        """Return effective timeout clamped within [1, 3600] seconds."""
        if timeout_sec is None:
            return self.default_timeout
        return max(1, min(int(timeout_sec), 3600))


@dataclass(slots=True)
class ParsedCodexOutput:
    """Structured fields parsed from `codex exec --json` JSONL output."""

    thread_id: str = ""
    agent_messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    noise_lines: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def summary_text(self) -> str:
        """Return the final agent message, falling back to errors."""
        for message in reversed(self.agent_messages):
            if message.strip():
                return message.strip()
        if self.errors:
            return "; ".join(self.errors)
        return ""


def parse_codex_jsonl(stdout_text: str) -> ParsedCodexOutput:
    """Parse newline-delimited JSON events emitted by `codex exec --json`."""
    parsed = ParsedCodexOutput()

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
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                parsed.thread_id = thread_id
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    parsed.agent_messages.append(text)
        elif event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key, val in usage.items():
                    if isinstance(val, int):
                        parsed.usage[key] = val
        elif event_type in ("error", "turn.failed"):
            message = event.get("message")
            if not isinstance(message, str) and isinstance(event.get("error"), dict):
                message = event["error"].get("message")
            if isinstance(message, str) and message:
                parsed.errors.append(message)

    return parsed


class CodexExecAdapter:
    """Adapter executing Codex sessions via `codex exec --json` with quota protection."""

    def __init__(self, config: CodexConfig) -> None:
        self.config = config

    def find_executable(self) -> str | None:
        """Locate the Codex CLI binary using explicit config or automatic discovery."""
        return discover_codex_executable(self.config.codex_bin)

    def validate_model(self, model: str | None) -> str:
        """Enforce the allowed-model quota policy unless escalation is enabled."""
        if model is None or not model.strip():
            return self.config.default_model
        candidate = model.strip()
        if self.config.allow_escalation or candidate in self.config.allowed_models:
            return candidate
        raise ValueError(
            f"Model '{candidate}' is blocked by quota policy. "
            f"Allowed Luna models: {list(self.config.allowed_models)}."
        )

    async def check_health(self) -> dict[str, Any]:
        """Probe Codex CLI availability and version."""
        executable = self.find_executable()
        if not executable:
            return {
                "available": False,
                "executable": None,
                "version": None,
                "error": "Codex CLI executable not found.",
            }

        cmd = wrap_command_for_platform([executable, "--version"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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

    async def run_session(
        self,
        *,
        prompt: str,
        cwd: str,
        sandbox: str = "workspace-write",
        model: str | None = None,
        worktree: bool = False,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Run a fresh Codex session inside validated cwd with enforced sandbox & model allowlist."""
        if not prompt or not prompt.strip():
            return self._failure("prompt must not be empty.", model=self.config.default_model)

        if sandbox not in VALID_CODEX_SANDBOXES:
            return self._failure(
                f"Sandbox '{sandbox}' is blocked by security policy. Allowed: {list(VALID_CODEX_SANDBOXES)}.",
                model=self.config.default_model,
            )

        try:
            validated_cwd = validate_workspace_dir(cwd, self.config.allowed_root)
        except PathSecurityError as exc:
            return self._failure(f"Security validation failed: {exc}", model=self.config.default_model, logs_tail=str(exc))

        try:
            resolved_model = self.validate_model(model)
        except ValueError as exc:
            return self._failure(str(exc), model=model or self.config.default_model)

        executable = self.find_executable()
        if not executable:
            return self._failure("Codex CLI executable was not found.", model=resolved_model)

        cmd = [
            executable,
            "exec",
            "-m",
            resolved_model,
            "-s",
            sandbox,
            "-C",
            str(validated_cwd),
            "--skip-git-repo-check",
            "--json",
        ]
        if self.config.ignore_user_config:
            cmd.append("--ignore-user-config")
        if worktree:
            cmd.append("--worktree")
        cmd.extend(self._config_override_args())
        cmd.append("-")

        return await self._execute_codex(
            cmd=wrap_command_for_platform(cmd),
            cwd=validated_cwd,
            prompt=prompt.strip(),
            resolved_model=resolved_model,
            timeout_sec=self.config.resolve_timeout(timeout_sec),
        )

    async def reply_session(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: str,
        model: str | None = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Resume an existing Codex thread by thread_id via `codex exec resume --json`."""
        clean_thread = thread_id.strip()
        if not clean_thread:
            return self._failure("thread_id must not be empty.", model=self.config.default_model)

        if not prompt or not prompt.strip():
            return self._failure("prompt must not be empty.", model=self.config.default_model, thread_id=clean_thread)

        try:
            validated_cwd = validate_workspace_dir(cwd, self.config.allowed_root)
        except PathSecurityError as exc:
            return self._failure(
                f"Security validation failed: {exc}",
                model=self.config.default_model,
                thread_id=clean_thread,
                logs_tail=str(exc),
            )

        try:
            resolved_model = self.validate_model(model)
        except ValueError as exc:
            return self._failure(str(exc), model=model or self.config.default_model, thread_id=clean_thread)

        executable = self.find_executable()
        if not executable:
            return self._failure("Codex CLI executable was not found.", model=resolved_model, thread_id=clean_thread)

        cmd = [
            executable,
            "exec",
            "resume",
            "-m",
            resolved_model,
            "--skip-git-repo-check",
            "--json",
        ]
        if self.config.ignore_user_config:
            cmd.append("--ignore-user-config")
        cmd.extend(self._config_override_args())
        cmd.extend([clean_thread, "-"])

        return await self._execute_codex(
            cmd=wrap_command_for_platform(cmd),
            cwd=validated_cwd,
            prompt=prompt.strip(),
            resolved_model=resolved_model,
            timeout_sec=self.config.resolve_timeout(timeout_sec),
            initial_thread_id=clean_thread,
        )

    def _config_override_args(self) -> list[str]:
        args = ["-c", f"approval_policy={self.config.approval_policy}"]
        if self.config.windows_sandbox:
            args.extend(["-c", f"windows.sandbox={self.config.windows_sandbox}"])
        return args

    async def _execute_codex(
        self,
        *,
        cmd: list[str],
        cwd: Path,
        prompt: str,
        resolved_model: str,
        timeout_sec: int,
        initial_thread_id: str = "",
    ) -> dict[str, Any]:
        before_snapshot = snapshot_workspace(cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=float(timeout_sec),
            )
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            artifacts = diff_artifacts(cwd, before_snapshot)
            return {
                "status": "timeout",
                "thread_id": initial_thread_id,
                "model": resolved_model,
                "summary": f"codex execution timed out after {timeout_sec}s and the process tree was terminated.",
                "artifacts": artifacts,
                "usage": None,
                "logs_tail": f"Process killed after timeout ({timeout_sec}s).",
                "exit_code": proc.returncode,
            }

        stdout_text = stdout_b.decode("utf-8", errors="replace")
        stderr_text = stderr_b.decode("utf-8", errors="replace")
        parsed = parse_codex_jsonl(stdout_text)
        artifacts = diff_artifacts(cwd, before_snapshot)

        effective_thread = parsed.thread_id or initial_thread_id
        succeeded = proc.returncode == 0 and not parsed.errors
        summary = parsed.summary_text or stderr_text.strip() or f"codex exited with status code {proc.returncode}."

        logs_parts: list[str] = []
        if parsed.noise_lines:
            logs_parts.append("\n".join(parsed.noise_lines))
        if stderr_text.strip():
            logs_parts.append(stderr_text.strip())
        logs_tail = truncate_tail("\n".join(logs_parts), self.config.max_log_chars)

        return {
            "status": "success" if succeeded else "failed",
            "thread_id": effective_thread,
            "model": resolved_model,
            "summary": summary,
            "artifacts": artifacts,
            "usage": parsed.usage or None,
            "logs_tail": logs_tail,
            "exit_code": proc.returncode,
        }

    @staticmethod
    def _failure(
        summary: str,
        *,
        model: str,
        thread_id: str = "",
        logs_tail: str = "",
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "thread_id": thread_id,
            "model": model,
            "summary": summary,
            "artifacts": [],
            "usage": None,
            "logs_tail": logs_tail,
            "exit_code": None,
        }
