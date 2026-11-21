"""Workspace path whitelist validation and artifact change detection."""

from __future__ import annotations

import os
from pathlib import Path

SKIP_SNAPSHOT_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"})


class PathSecurityError(ValueError):
    """Raised when a requested path violates the allowed-root security boundary."""


def _ensure_within_allowed_root(candidate: Path, allowed_root: Path, label: str) -> Path:
    root_resolved = allowed_root.resolve()
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise PathSecurityError(
            f"{label} '{candidate}' resolves outside allowed root '{root_resolved}'."
        )

    # Guard against existing ancestor symlinks pointing outside allowed_root
    # when the leaf file itself does not exist yet.
    probe = candidate
    while True:
        if probe.exists() or probe.is_symlink():
            if not probe.resolve(strict=False).is_relative_to(root_resolved):
                raise PathSecurityError(
                    f"{label} ancestor '{probe}' escapes allowed root '{root_resolved}'."
                )
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    return resolved


def validate_workspace_dir(workspace_dir: str, allowed_root: Path) -> Path:
    """Validate that workspace_dir is an existing absolute directory inside allowed_root."""
    if not workspace_dir or not workspace_dir.strip():
        raise PathSecurityError("workspace_dir must not be empty.")

    raw_path = Path(workspace_dir.strip())
    if not raw_path.is_absolute():
        raise PathSecurityError(f"workspace_dir must be an absolute path: '{workspace_dir}'.")

    resolved = _ensure_within_allowed_root(raw_path, allowed_root, "workspace_dir")
    if not resolved.exists() or not resolved.is_dir():
        raise PathSecurityError(f"workspace_dir does not exist or is not a directory: '{resolved}'.")
    return resolved


def validate_output_file(
    output_file: str | None,
    workspace_dir: Path,
    allowed_root: Path,
) -> Path | None:
    """Validate optional output_file stays inside allowed_root (anchoring relative paths to workspace_dir)."""
    if output_file is None or not output_file.strip():
        return None

    candidate = Path(output_file.strip())
    if not candidate.is_absolute():
        candidate = workspace_dir / candidate

    return _ensure_within_allowed_root(candidate, allowed_root, "output_file")


def snapshot_workspace(workspace_dir: Path, max_files: int = 5000) -> dict[str, tuple[int, int]]:
    """Capture a bounded (mtime_ns, size) snapshot of regular files under workspace_dir."""
    snapshot: dict[str, tuple[int, int]] = {}
    root = str(workspace_dir)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_SNAPSHOT_DIRS]
        for filename in filenames:
            if len(snapshot) >= max_files:
                return snapshot
            full_path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            snapshot[os.path.abspath(full_path)] = (stat.st_mtime_ns, stat.st_size)

    return snapshot


def diff_artifacts(
    workspace_dir: Path,
    before: dict[str, tuple[int, int]],
    output_file: Path | None = None,
    max_files: int = 5000,
) -> list[str]:
    """Return sorted paths of files created or modified since the pre-execution snapshot."""
    after = snapshot_workspace(workspace_dir, max_files=max_files)
    changed = {
        path
        for path, meta in after.items()
        if path not in before or before[path] != meta
    }

    if output_file is not None and output_file.exists():
        changed.add(os.path.abspath(str(output_file)))

    return sorted(changed)
