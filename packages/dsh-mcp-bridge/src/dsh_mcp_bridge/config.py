"""Configuration management for dsh-mcp-bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

VALID_MODEL_TIERS = ("flash", "reasoner", "default")
VALID_APPROVAL_POLICIES = ("never", "ask")


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse common boolean environment strings."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    """Parse and clamp an integer environment string within [minimum, maximum]."""
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def resolve_allowed_root(env: Mapping[str, str], *keys: str) -> Path:
    """Resolve the first non-empty path from env keys, defaulting to CWD."""
    for key in keys:
        raw = env.get(key, "").strip()
        if raw:
            return Path(raw).resolve()
    return Path.cwd().resolve()


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Runtime settings for dsh-mcp-bridge loaded from environment variables."""

    dsh_bin: str = "dsh"
    dsh_base_url: str = "http://127.0.0.1:3080"
    allowed_root: Path = Path(".")
    default_timeout: int = 300
    max_sync_wait_sec: int = 120
    max_log_chars: int = 4000
    dsh_home: str | None = None
    approval_policy: str = "never"
    model_flash: str = "deepseek-flash"
    model_reasoner: str = "deepseek-v4-pro"
    model_default: str = "deepseek-flash"
    deepseek_api_key: str | None = None
    deepseek_api_base: str = "https://api.deepseek.com"
    allow_api_fallback: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BridgeConfig:
        """Build a BridgeConfig from environment variables (defaults to os.environ)."""
        env = os.environ if environ is None else environ

        allowed_root = resolve_allowed_root(env, "DSH_ALLOWED_ROOT")

        raw_home = env.get("DSH_HOME", "").strip()
        dsh_home = raw_home if raw_home else None

        raw_policy = env.get("DSH_APPROVAL_POLICY", "never").strip().lower()
        approval_policy = raw_policy if raw_policy in VALID_APPROVAL_POLICIES else "never"

        raw_key = env.get("DEEPSEEK_API_KEY", "").strip()
        deepseek_api_key = raw_key if raw_key else None

        return cls(
            dsh_bin=env.get("DSH_BIN", "dsh").strip() or "dsh",
            dsh_base_url=(env.get("DSH_BASE_URL", "http://127.0.0.1:3080").strip() or "http://127.0.0.1:3080").rstrip("/"),
            allowed_root=allowed_root,
            default_timeout=parse_int(env.get("DSH_DEFAULT_TIMEOUT"), 300, 1, 3600),
            max_sync_wait_sec=parse_int(env.get("DSH_MAX_SYNC_WAIT_SEC"), 120, 0, 150),
            max_log_chars=parse_int(env.get("DSH_MAX_LOG_CHARS"), 4000, 256, 100_000),
            dsh_home=dsh_home,
            approval_policy=approval_policy,
            model_flash=env.get("DSH_MODEL_FLASH", "deepseek-flash").strip() or "deepseek-flash",
            model_reasoner=env.get("DSH_MODEL_REASONER", "deepseek-v4-pro").strip() or "deepseek-v4-pro",
            model_default=env.get("DSH_MODEL_DEFAULT", "deepseek-flash").strip() or "deepseek-flash",
            deepseek_api_key=deepseek_api_key,
            deepseek_api_base=(env.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").strip() or "https://api.deepseek.com").rstrip("/"),
            allow_api_fallback=parse_bool(env.get("DSH_ALLOW_API_FALLBACK"), default=False),
        )

    def resolve_model_name(self, model_tier: str) -> str:
        """Map a logical model tier ('flash', 'reasoner', 'default') to a dsh model ID."""
        normalized = model_tier.strip().lower()
        mapping = {
            "flash": self.model_flash,
            "reasoner": self.model_reasoner,
            "default": self.model_default,
        }
        if normalized not in mapping:
            raise ValueError(f"Unsupported model_tier '{model_tier}'. Expected one of {VALID_MODEL_TIERS}.")
        return mapping[normalized]

    def resolve_timeout(self, timeout_sec: int | None) -> int:
        """Return effective timeout clamped within [1, 3600] seconds."""
        if timeout_sec is None:
            return self.default_timeout
        return max(1, min(int(timeout_sec), 3600))
