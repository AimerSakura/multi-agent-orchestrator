"""Stderr-only logging, log truncation, and trajectory index persistence."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes exclusively to stderr so MCP stdout stays clean."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def truncate_tail(text: str, max_chars: int) -> str:
    """Keep at most the trailing max_chars of text, prefixing a truncation marker when cut."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "...[truncated]\n"
    if max_chars <= len(marker):
        return text[-max_chars:]
    return marker + text[-(max_chars - len(marker)):]


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TrajectoryRecord:
    """Metadata stored per bridge execution to enable resume/fork lookups."""

    trajectory_id: str
    workspace_dir: str
    adapter: str
    model_tier: str
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    parent_trajectory_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryRecord:
        """Deserialize a TrajectoryRecord from JSON dictionary data."""
        return cls(
            trajectory_id=str(data["trajectory_id"]),
            workspace_dir=str(data["workspace_dir"]),
            adapter=str(data.get("adapter", "dsh-cli")),
            model_tier=str(data.get("model_tier", "default")),
            status=str(data.get("status", "unknown")),
            summary=str(data.get("summary", "")),
            artifacts=[str(item) for item in data.get("artifacts", [])],
            parent_trajectory_id=data.get("parent_trajectory_id"),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
        )


class TrajectoryStore:
    """JSON-backed persistence for dsh trajectory metadata under .agent-handoff/."""

    def __init__(self, allowed_root: Path) -> None:
        self.index_path = allowed_root.resolve() / ".agent-handoff" / ".dsh-bridge" / "trajectories.json"
        self._logger = get_logger("dsh_mcp_bridge.trajectory")

    def _load_all(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        try:
            raw = self.index_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("Failed to load trajectory index %s: %s", self.index_path, exc)
        return {}

    def _save_all(self, data: dict[str, dict[str, Any]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".trajectories-",
            suffix=".tmp",
            dir=str(self.index_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.index_path)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def get(self, trajectory_id: str) -> TrajectoryRecord | None:
        """Retrieve a saved record by trajectory_id, or None if missing."""
        data = self._load_all()
        entry = data.get(trajectory_id)
        if not isinstance(entry, dict):
            return None
        return TrajectoryRecord.from_dict(entry)

    def upsert(self, record: TrajectoryRecord) -> None:
        """Insert or update a trajectory record atomically."""
        data = self._load_all()
        existing = data.get(record.trajectory_id)
        if isinstance(existing, dict) and "created_at" in existing:
            record.created_at = str(existing["created_at"])
        record.updated_at = utc_now_iso()
        data[record.trajectory_id] = asdict(record)
        self._save_all(data)

    def list_recent(self, limit: int = 20) -> list[TrajectoryRecord]:
        """Return up to `limit` most recently updated trajectory records."""
        records = [
            TrajectoryRecord.from_dict(item)
            for item in self._load_all().values()
            if isinstance(item, dict) and "trajectory_id" in item
        ]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]
