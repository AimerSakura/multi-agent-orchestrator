"""Codex CLI (`app-server` / `exec --json`) execution adapter and quota-lock configuration."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

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
VALID_CODEX_BACKENDS = ("app-server", "exec")
DEFAULT_CODEX_BACKEND = "app-server"
DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_ALLOWED_CODEX_MODELS = ("gpt-6-luna", "gpt-5.6-luna")
APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024


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
    max_sync_wait_sec: int = 120
    max_log_chars: int = 4000
    ignore_user_config: bool = True
    approval_policy: str = "never"
    windows_sandbox: str | None = None
    backend: str = DEFAULT_CODEX_BACKEND

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
            windows_sandbox: str | None = None
        else:
            cleaned = raw_win_sandbox.strip().lower()
            windows_sandbox = None if cleaned in {"", "none", "off"} else cleaned

        raw_backend = env.get("CODEX_BACKEND", DEFAULT_CODEX_BACKEND).strip().lower()
        backend = raw_backend if raw_backend in VALID_CODEX_BACKENDS else DEFAULT_CODEX_BACKEND

        return cls(
            codex_bin=codex_bin,
            allowed_root=allowed_root,
            default_model=default_model,
            allowed_models=tuple(dict.fromkeys(parsed_models)),
            allow_escalation=parse_bool(env.get("CODEX_ALLOW_ESCALATION"), default=False),
            default_timeout=parse_int(env.get("CODEX_DEFAULT_TIMEOUT"), 300, 1, 3600),
            max_sync_wait_sec=parse_int(env.get("CODEX_MAX_SYNC_WAIT_SEC"), 120, 0, 150),
            max_log_chars=parse_int(env.get("CODEX_MAX_LOG_CHARS"), 4000, 256, 100_000),
            ignore_user_config=parse_bool(env.get("CODEX_IGNORE_USER_CONFIG"), default=True),
            approval_policy=approval_policy,
            windows_sandbox=windows_sandbox,
            backend=backend,
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


def _truncate_text(text: str, max_chars: int) -> str:
    """Keep at most max_chars from the beginning of text, appending a truncation marker."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n...[truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker


def _sandbox_to_turn_policy(sandbox: str, cwd: Path, *, windows_sandbox: str | None = None) -> dict[str, Any]:
    """Convert MCP sandbox string to Codex app-server v2 SandboxPolicy object."""
    if os.name == "nt" and windows_sandbox is None:
        return {"type": "dangerFullAccess"}
    if sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if sandbox == "workspace-write":
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(cwd)],
            "networkAccess": False,
        }
    raise ValueError(f"Unsupported sandbox mode '{sandbox}'. Expected 'read-only' or 'workspace-write'.")


def _normalize_app_server_usage(token_usage: Any) -> dict[str, int] | None:
    """Normalize app-server camelCase ThreadTokenUsage into snake_case token counts."""
    if not isinstance(token_usage, Mapping):
        return None
    bucket = token_usage.get("last")
    if not isinstance(bucket, Mapping):
        bucket = token_usage.get("total")
    if not isinstance(bucket, Mapping):
        return None

    key_map = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
        "totalTokens": "total_tokens",
    }
    normalized: dict[str, int] = {}
    for src_key, dst_key in key_map.items():
        val = bucket.get(src_key)
        if isinstance(val, int) and not isinstance(val, bool):
            normalized[dst_key] = val
    return normalized or None


def _summarize_thread_item(item: Mapping[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Produce a concise structured summary for a Codex ThreadItem in `thread/read`."""
    item_type = item.get("type")
    if item_type == "userMessage":
        parts = item.get("content")
        if not isinstance(parts, list):
            return None
        joined = "\n".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, Mapping) and isinstance(part.get("text"), str) and part.get("text")
        ).strip()
        if not joined:
            return None
        return {"type": "userMessage", "text": _truncate_text(joined, max_chars)}

    if item_type == "agentMessage":
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return {"type": "agentMessage", "text": _truncate_text(text.strip(), max_chars)}

    if item_type == "commandExecution":
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        return {
            "type": "commandExecution",
            "command": _truncate_text(command.strip(), max_chars),
            "status": item.get("status"),
            "exit_code": item.get("exitCode"),
        }

    if item_type == "fileChange":
        changes = item.get("changes")
        paths: list[str] = []
        if isinstance(changes, list):
            for ch in changes:
                if isinstance(ch, Mapping) and isinstance(ch.get("path"), str):
                    paths.append(ch["path"])
        return {
            "type": "fileChange",
            "status": item.get("status"),
            "paths": paths,
        }

    return None


def _format_thread_summary(thread: Mapping[str, Any]) -> dict[str, Any]:
    """Extract normalized thread metadata returned by `thread/list` or `thread/read`."""
    preview = thread.get("preview")
    name = thread.get("name")
    title = name if isinstance(name, str) and name.strip() else (preview if isinstance(preview, str) else "")
    return {
        "thread_id": str(thread.get("id", "")),
        "title": title,
        "preview": preview if isinstance(preview, str) else "",
        "cwd": str(thread.get("cwd", "")),
        "source": thread.get("source"),
        "originator": thread.get("originator"),
        "model": thread.get("model"),
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt"),
        "path": thread.get("path"),
    }


def _extract_rpc_error(resp: Mapping[str, Any]) -> str | None:
    """Return formatted JSON-RPC error string if present on response."""
    err = resp.get("error")
    if isinstance(err, Mapping):
        msg = err.get("message")
        return str(msg) if msg else json.dumps(err, ensure_ascii=False)
    if err is not None:
        return str(err)
    return None


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
        on_thread_id: Callable[[str], None] | None = None,
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

        effective_timeout = self.config.resolve_timeout(timeout_sec)
        if self.config.backend == "app-server" and not worktree:
            return await self._run_app_server_turn(
                mode="start",
                executable=executable,
                cwd=validated_cwd,
                prompt=prompt.strip(),
                resolved_model=resolved_model,
                sandbox=sandbox,
                timeout_sec=effective_timeout,
                on_thread_id=on_thread_id,
            )

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
            timeout_sec=effective_timeout,
        )

    async def reply_session(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: str,
        model: str | None = None,
        sandbox: str | None = None,
        timeout_sec: int | None = None,
        on_thread_id: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Resume an existing Codex thread by thread_id via `app-server` or `exec resume`."""
        clean_thread = thread_id.strip()
        if not clean_thread:
            return self._failure("thread_id must not be empty.", model=self.config.default_model)

        if not prompt or not prompt.strip():
            return self._failure("prompt must not be empty.", model=self.config.default_model, thread_id=clean_thread)

        if sandbox is not None and sandbox not in VALID_CODEX_SANDBOXES:
            return self._failure(
                f"Sandbox '{sandbox}' is blocked by security policy. Allowed: {list(VALID_CODEX_SANDBOXES)}.",
                model=self.config.default_model,
                thread_id=clean_thread,
            )

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

        effective_timeout = self.config.resolve_timeout(timeout_sec)
        if self.config.backend == "app-server":
            return await self._run_app_server_turn(
                mode="resume",
                executable=executable,
                cwd=validated_cwd,
                prompt=prompt.strip(),
                resolved_model=resolved_model,
                sandbox=sandbox,
                timeout_sec=effective_timeout,
                thread_id=clean_thread,
                on_thread_id=on_thread_id,
            )

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
            timeout_sec=effective_timeout,
            initial_thread_id=clean_thread,
        )

    async def _call_app_server_method(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
        timeout_sec: int,
        empty_key: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Run a single request/response method over an ephemeral `codex app-server` connection."""
        executable = self.find_executable()
        if not executable:
            return None, {"status": "failed", "summary": "Codex CLI executable was not found.", empty_key: []}

        proc: asyncio.subprocess.Process | None = None
        try:
            async def _fetch() -> dict[str, Any]:
                nonlocal proc
                proc = await self._start_app_server(executable=executable, cwd=self.config.allowed_root)
                await self._app_server_handshake(proc)
                return await self._app_server_rpc_request(proc, 2, method, params)

            resp = await asyncio.wait_for(_fetch(), timeout=float(max(1, timeout_sec)))
        except asyncio.TimeoutError:
            if proc is not None:
                await kill_process_tree(proc)
            return None, {
                "status": "timeout",
                "summary": f"codex {method} timed out after {timeout_sec}s.",
                empty_key: [],
            }
        except Exception as exc:
            return None, {"status": "failed", "summary": f"codex {method} failed: {exc}", empty_key: []}
        finally:
            if proc is not None:
                await self._shutdown_app_server(proc)

        rpc_err = _extract_rpc_error(resp)
        if rpc_err is not None:
            return None, {"status": "failed", "summary": f"codex {method} error: {rpc_err}", empty_key: []}
        return resp, None

    async def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 20,
        include_exec: bool = False,
        search_term: str | None = None,
        timeout_sec: int = 20,
    ) -> dict[str, Any]:
        """List persistent Codex threads visible in Codex CLI/Desktop app-server."""
        clamped_limit = max(1, min(int(limit), 100))
        params: dict[str, Any] = {"limit": clamped_limit}
        if cwd and cwd.strip():
            params["cwd"] = str(Path(cwd.strip()).resolve())
        if search_term and search_term.strip():
            params["searchTerm"] = search_term.strip()
        if include_exec:
            params["sourceKinds"] = ["vscode", "cli", "appServer", "exec"]

        resp, failure = await self._call_app_server_method(
            method="thread/list",
            params=params,
            timeout_sec=timeout_sec,
            empty_key="threads",
        )
        if failure is not None or resp is None:
            return failure or {"status": "failed", "summary": "codex thread/list failed.", "threads": []}

        raw_threads = (resp.get("result") or {}).get("data")
        threads = [
            _format_thread_summary(t)
            for t in (raw_threads if isinstance(raw_threads, list) else [])
            if isinstance(t, Mapping)
        ]
        return {"status": "success", "count": len(threads), "threads": threads}

    async def read_thread(
        self,
        *,
        thread_id: str,
        max_turns: int = 20,
        max_chars_per_message: int = 4000,
        timeout_sec: int = 20,
    ) -> dict[str, Any]:
        """Read full turn/message history of an existing Codex thread by thread_id."""
        clean_id = thread_id.strip()
        if not clean_id:
            return {"status": "failed", "summary": "thread_id must not be empty.", "turns": []}

        clamped_turns = max(1, min(int(max_turns), 100))
        clamped_chars = max(200, min(int(max_chars_per_message), 20000))

        resp, failure = await self._call_app_server_method(
            method="thread/read",
            params={"threadId": clean_id, "includeTurns": True},
            timeout_sec=timeout_sec,
            empty_key="turns",
        )
        if failure is not None or resp is None:
            return failure or {"status": "failed", "summary": "codex thread/read failed.", "turns": []}

        thread_obj = (resp.get("result") or {}).get("thread")
        if not isinstance(thread_obj, Mapping):
            return {"status": "failed", "summary": f"Thread '{clean_id}' returned no data.", "turns": []}

        raw_turns = thread_obj.get("turns")
        all_turns = [t for t in raw_turns if isinstance(t, Mapping)] if isinstance(raw_turns, list) else []
        selected_turns = all_turns[-clamped_turns:]
        formatted_turns: list[dict[str, Any]] = []
        for turn in selected_turns:
            raw_items = turn.get("items")
            items: list[dict[str, Any]] = []
            if isinstance(raw_items, list):
                for raw_item in raw_items:
                    if isinstance(raw_item, Mapping):
                        summarized = _summarize_thread_item(raw_item, clamped_chars)
                        if summarized is not None:
                            items.append(summarized)
            formatted_turns.append(
                {
                    "turn_id": turn.get("id"),
                    "status": turn.get("status"),
                    "items": items,
                }
            )

        return {
            "status": "success",
            "thread": _format_thread_summary(thread_obj),
            "total_turns": len(all_turns),
            "returned_turns": len(formatted_turns),
            "turns": formatted_turns,
        }

    def _config_override_args(self) -> list[str]:
        args = ["-c", f"approval_policy={self.config.approval_policy}"]
        if self.config.windows_sandbox:
            args.extend(["-c", f"windows.sandbox={self.config.windows_sandbox}"])
        return args

    async def _start_app_server(
        self,
        *,
        executable: str,
        cwd: Path,
        resolved_model: str | None = None,
    ) -> asyncio.subprocess.Process:
        cmd = [executable, "app-server", "--listen", "stdio://"]
        if resolved_model:
            cmd.extend(["-c", f'model="{resolved_model}"'])
        cmd.extend(["-c", f'approval_policy="{self.config.approval_policy}"'])
        if self.config.windows_sandbox:
            cmd.extend(["-c", f'windows.sandbox="{self.config.windows_sandbox}"'])

        return await asyncio.create_subprocess_exec(
            *wrap_command_for_platform(cmd),
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
        )

    @staticmethod
    async def _app_server_rpc_send(proc: asyncio.subprocess.Process, payload: Mapping[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin pipe is unavailable.")
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    @staticmethod
    async def _read_stderr_snippet(proc: asyncio.subprocess.Process) -> str:
        if proc.stderr is None:
            return ""
        try:
            stderr_b = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
            return stderr_b.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    async def _app_server_next_message(
        self,
        proc: asyncio.subprocess.Process,
        *,
        context: str,
    ) -> dict[str, Any]:
        if proc.stdout is None:
            raise RuntimeError("codex app-server stdout pipe is unavailable.")
        while True:
            raw_line = await proc.stdout.readline()
            if not raw_line:
                stderr_text = await self._read_stderr_snippet(proc)
                detail = f"; stderr: {stderr_text}" if stderr_text else ""
                raise RuntimeError(f"codex app-server closed stdout unexpectedly ({context}){detail}")

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue

            # Reject server-to-client interactive requests immediately to avoid deadlocking the turn.
            if "id" in msg and "method" in msg:
                await self._app_server_rpc_send(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": (
                                f"Unsupported interactive server request '{msg['method']}' "
                                "in non-interactive MCP bridge."
                            ),
                        },
                    },
                )
                continue

            return msg

    async def _app_server_read_until_response(
        self,
        proc: asyncio.subprocess.Process,
        req_id: int,
        *,
        on_notification: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        while True:
            msg = await self._app_server_next_message(proc, context=f"waiting for response id={req_id}")
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
            if "method" in msg and on_notification is not None:
                on_notification(msg)

    async def _app_server_rpc_request(
        self,
        proc: asyncio.subprocess.Process,
        req_id: int,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        on_notification: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._app_server_rpc_send(proc, payload)
        return await self._app_server_read_until_response(proc, req_id, on_notification=on_notification)

    async def _app_server_handshake(self, proc: asyncio.subprocess.Process) -> None:
        init_resp = await self._app_server_rpc_request(
            proc,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "multi-agent-orchestrator",
                    "title": "Multi-Agent Orchestrator",
                    "version": "0.2.0",
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        rpc_err = _extract_rpc_error(init_resp)
        if rpc_err is not None:
            raise RuntimeError(f"initialize failed: {rpc_err}")
        await self._app_server_rpc_send(proc, {"jsonrpc": "2.0", "method": "initialized"})

    async def _shutdown_app_server(self, proc: asyncio.subprocess.Process) -> str:
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                await kill_process_tree(proc)
        return await self._read_stderr_snippet(proc)

    async def _run_app_server_turn(
        self,
        *,
        mode: Literal["start", "resume"],
        executable: str,
        cwd: Path,
        prompt: str,
        resolved_model: str,
        sandbox: str | None,
        timeout_sec: int,
        thread_id: str = "",
        on_thread_id: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        before_snapshot = snapshot_workspace(cwd)
        proc: asyncio.subprocess.Process | None = None
        effective_thread_id = thread_id
        thread_source: Any = None
        agent_messages: list[str] = []
        errors: list[str] = []
        retried_warnings: list[str] = []
        usage: dict[str, int] | None = None
        turn_status = ""
        fatal_turn_error: str | None = None

        def _handle_notification(msg: dict[str, Any]) -> None:
            nonlocal usage
            method = msg.get("method")
            params = msg.get("params")
            if not isinstance(params, Mapping):
                return
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str):
                        agent_messages.append(text)
            elif method == "thread/tokenUsage/updated":
                normalized = _normalize_app_server_usage(params.get("tokenUsage"))
                if normalized is not None:
                    usage = normalized
            elif method == "error":
                err_obj = params.get("error")
                err_msg = err_obj.get("message") if isinstance(err_obj, Mapping) else params.get("message")
                if isinstance(err_msg, str) and err_msg:
                    if params.get("willRetry") is True:
                        retried_warnings.append(err_msg)
                    else:
                        errors.append(err_msg)

        async def _execute_flow() -> dict[str, Any] | None:
            nonlocal proc, effective_thread_id, thread_source, turn_status, fatal_turn_error
            proc = await self._start_app_server(
                executable=executable,
                cwd=cwd,
                resolved_model=resolved_model,
            )
            await self._app_server_handshake(proc)

            if mode == "start":
                thread_params: dict[str, Any] = {
                    "model": resolved_model,
                    "cwd": str(cwd),
                    "sandbox": sandbox,
                    "approvalPolicy": self.config.approval_policy,
                    "ephemeral": False,
                }
                thread_resp = await self._app_server_rpc_request(
                    proc, 2, "thread/start", thread_params, on_notification=_handle_notification
                )
            else:
                thread_params = {
                    "threadId": thread_id,
                    "model": resolved_model,
                    "cwd": str(cwd),
                    "approvalPolicy": self.config.approval_policy,
                }
                if sandbox is not None:
                    thread_params["sandbox"] = sandbox
                thread_resp = await self._app_server_rpc_request(
                    proc, 2, "thread/resume", thread_params, on_notification=_handle_notification
                )

            thread_err = _extract_rpc_error(thread_resp)
            if thread_err is not None:
                return {"rpc_error": f"{'thread/start' if mode == 'start' else 'thread/resume'} failed: {thread_err}"}

            thread_obj = (thread_resp.get("result") or {}).get("thread")
            if isinstance(thread_obj, Mapping):
                effective_thread_id = str(thread_obj.get("id") or effective_thread_id)
                thread_source = thread_obj.get("source")
            if on_thread_id is not None and effective_thread_id:
                on_thread_id(effective_thread_id)

            turn_params: dict[str, Any] = {
                "threadId": effective_thread_id,
                "model": resolved_model,
                "cwd": str(cwd),
                "approvalPolicy": self.config.approval_policy,
                "input": [{"type": "text", "text": prompt}],
            }
            if sandbox is not None:
                turn_params["sandboxPolicy"] = _sandbox_to_turn_policy(
            sandbox, cwd, windows_sandbox=self.config.windows_sandbox
        )

            turn_resp = await self._app_server_rpc_request(
                proc, 3, "turn/start", turn_params, on_notification=_handle_notification
            )
            turn_rpc_err = _extract_rpc_error(turn_resp)
            if turn_rpc_err is not None:
                return {"rpc_error": f"turn/start failed: {turn_rpc_err}"}

            while True:
                msg = await self._app_server_next_message(proc, context="waiting for turn/completed")
                method = msg.get("method")
                if not method:
                    continue
                _handle_notification(msg)
                if method == "turn/completed":
                    params = msg.get("params")
                    if isinstance(params, Mapping):
                        turn = params.get("turn")
                        if isinstance(turn, Mapping):
                            turn_status = str(turn.get("status") or "")
                            turn_err = turn.get("error")
                            if isinstance(turn_err, Mapping):
                                fatal_turn_error = str(
                                    turn_err.get("message") or json.dumps(turn_err, ensure_ascii=False)
                                )
                            elif turn_err is not None:
                                fatal_turn_error = str(turn_err)
                    break
            return None

        flow_issue: dict[str, Any] | None = None
        stderr_snippet = ""
        try:
            flow_issue = await asyncio.wait_for(_execute_flow(), timeout=float(timeout_sec))
        except asyncio.TimeoutError:
            if proc is not None:
                await kill_process_tree(proc)
            artifacts = diff_artifacts(cwd, before_snapshot)
            return {
                "status": "timeout",
                "thread_id": effective_thread_id,
                "model": resolved_model,
                "summary": f"codex app-server execution timed out after {timeout_sec}s and the process tree was terminated.",
                "artifacts": artifacts,
                "usage": usage,
                "logs_tail": f"Process killed after timeout ({timeout_sec}s).",
                "exit_code": proc.returncode if proc is not None else None,
                "backend": "app-server",
                "source": thread_source,
            }
        except asyncio.CancelledError:
            if proc is not None:
                await kill_process_tree(proc)
                proc = None
            raise
        except Exception as exc:
            flow_issue = {"rpc_error": str(exc)}
        finally:
            if proc is not None:
                stderr_snippet = await self._shutdown_app_server(proc)

        artifacts = diff_artifacts(cwd, before_snapshot)
        if flow_issue is not None:
            err_text = str(flow_issue.get("rpc_error") or "codex app-server failed.")
            logs_tail = truncate_tail(
                "\n".join(part for part in [err_text, stderr_snippet] if part),
                self.config.max_log_chars,
            )
            return {
                "status": "failed",
                "thread_id": effective_thread_id,
                "model": resolved_model,
                "summary": err_text,
                "artifacts": artifacts,
                "usage": usage,
                "logs_tail": logs_tail,
                "exit_code": 1,
                "backend": "app-server",
                "source": thread_source,
            }

        if fatal_turn_error:
            errors.append(fatal_turn_error)

        succeeded = turn_status == "completed" and fatal_turn_error is None and not errors
        summary = ""
        for message in reversed(agent_messages):
            if message.strip():
                summary = message.strip()
                break
        if not summary:
            if errors:
                summary = "; ".join(errors)
            elif stderr_snippet:
                summary = stderr_snippet
            else:
                summary = f"codex app-server turn finished with status '{turn_status or 'unknown'}'."

        logs_parts = [*retried_warnings, *errors]
        if stderr_snippet:
            logs_parts.append(stderr_snippet)
        logs_tail = truncate_tail("\n".join(logs_parts), self.config.max_log_chars)

        return {
            "status": "success" if succeeded else "failed",
            "thread_id": effective_thread_id,
            "model": resolved_model,
            "summary": summary,
            "artifacts": artifacts,
            "usage": usage,
            "logs_tail": logs_tail,
            "exit_code": 0 if succeeded else 1,
            "backend": "app-server",
            "source": thread_source,
        }

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
                "backend": "exec",
            }
        except asyncio.CancelledError:
            await kill_process_tree(proc)
            raise

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
            "backend": "exec",
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
