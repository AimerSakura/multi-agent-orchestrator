"""Tests for workspace path whitelist validation and artifact diff detection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dsh_mcp_bridge.security import (
    PathSecurityError,
    diff_artifacts,
    snapshot_workspace,
    validate_output_file,
    validate_workspace_dir,
)


def test_validate_workspace_dir_accepts_allowed_subdir(tmp_path: Path) -> None:
    allowed = tmp_path / "root"
    sub = allowed / "project-a"
    sub.mkdir(parents=True)

    result = validate_workspace_dir(str(sub), allowed)
    assert result == sub.resolve()


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "   ",
        "relative/path",
        ".",
    ],
)
def test_validate_workspace_dir_rejects_empty_or_relative(tmp_path: Path, bad_path: str) -> None:
    with pytest.raises(PathSecurityError):
        validate_workspace_dir(bad_path, tmp_path)


def test_validate_workspace_dir_rejects_traversal_outside_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    traversal = str(allowed / ".." / "outside")
    with pytest.raises(PathSecurityError):
        validate_workspace_dir(traversal, allowed)


def test_validate_workspace_dir_rejects_nonexistent_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(PathSecurityError):
        validate_workspace_dir(str(missing), tmp_path)


def test_validate_workspace_dir_rejects_regular_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("hi", encoding="utf-8")
    with pytest.raises(PathSecurityError):
        validate_workspace_dir(str(file_path), tmp_path)


def test_validate_output_file_anchors_relative_and_rejects_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "repo"
    workspace.mkdir(parents=True)

    rel_out = validate_output_file(".agent-handoff/task-1/report.md", workspace, allowed)
    assert rel_out == (workspace / ".agent-handoff/task-1/report.md").resolve(strict=False)

    abs_out = validate_output_file(str(allowed / "shared.json"), workspace, allowed)
    assert abs_out == (allowed / "shared.json").resolve(strict=False)

    assert validate_output_file("   ", workspace, allowed) is None
    assert validate_output_file(None, workspace, allowed) is None

    with pytest.raises(PathSecurityError):
        validate_output_file("../../escape.txt", workspace, allowed)

    with pytest.raises(PathSecurityError):
        validate_output_file(str(tmp_path / "outside.txt"), workspace, allowed)


def test_snapshot_and_diff_artifacts_detects_new_and_modified_files(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("v1", encoding="utf-8")
    ignored_git = tmp_path / ".git"
    ignored_git.mkdir()
    (ignored_git / "config").write_text("ignored", encoding="utf-8")

    before = snapshot_workspace(tmp_path)
    assert os.path.abspath(str(existing)) in before
    assert os.path.abspath(str(ignored_git / "config")) not in before

    created = tmp_path / "out" / "result.json"
    created.parent.mkdir()
    created.write_text('{"ok": true}', encoding="utf-8")
    existing.write_text("v2-updated", encoding="utf-8")

    artifacts = diff_artifacts(tmp_path, before, output_file=created)
    assert artifacts == sorted([os.path.abspath(str(existing)), os.path.abspath(str(created))])
