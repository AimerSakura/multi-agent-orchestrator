"""Tests for native DeepSeek Harness (`dsh`) session listing and history reading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dsh_mcp_bridge import dsh_sessions
from dsh_mcp_bridge.dsh_sessions import list_dsh_sessions, read_dsh_session


def _write_projcache(
    dsh_home: Path,
    *,
    session_id: str,
    cwd: str,
    created_at_ms: int,
    title: str,
    turns: list[dict[str, Any]],
) -> None:
    proj_dir = dsh_home / "storages" / "session_projcache" / "sessions"
    proj_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "record": {
            "identity": {
                "formatVersion": 4,
                "createdAt": created_at_ms,
                "cwd": cwd,
            },
            "rows": {
                "title": {"ver": 1, "seq": 10, "val": title},
                "modelSelection": {"ver": 1, "seq": 10, "val": "deepseek-flash"},
                "turnOutline": {"ver": 2, "seq": 10, "val": {"turns": turns, "draft": ""}},
            },
        },
    }
    (proj_dir / f"{session_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_list_dsh_sessions_sorts_and_filters_by_workspace_and_search(tmp_path: Path) -> None:
    ws_a = tmp_path / "repo-a"
    ws_b = tmp_path / "repo-b"
    ws_a.mkdir()
    ws_b.mkdir()

    dsh_home = tmp_path / "dsh_home"
    _write_projcache(
        dsh_home,
        session_id="session-old",
        cwd=str(ws_a),
        created_at_ms=1000,
        title="Alpha setup",
        turns=[{"turn": 1, "prompt": "init", "response": "done"}],
    )
    _write_projcache(
        dsh_home,
        session_id="session-new",
        cwd=str(ws_a),
        created_at_ms=2000,
        title="Beta checklist",
        turns=[{"turn": 1, "prompt": "write checklist", "response": "ok"}],
    )
    _write_projcache(
        dsh_home,
        session_id="session-other",
        cwd=str(ws_b),
        created_at_ms=3000,
        title="Unrelated project",
        turns=[],
    )

    all_res = list_dsh_sessions(dsh_home)
    assert all_res["status"] == "success"
    assert [s["session_id"] for s in all_res["sessions"]] == ["session-other", "session-new", "session-old"]

    filtered_ws = list_dsh_sessions(dsh_home, workspace_dir=str(ws_a))
    assert [s["session_id"] for s in filtered_ws["sessions"]] == ["session-new", "session-old"]

    filtered_search = list_dsh_sessions(dsh_home, workspace_dir=str(ws_a), search_term="checklist")
    assert [s["session_id"] for s in filtered_search["sessions"]] == ["session-new"]


def test_read_dsh_session_falls_back_to_projcache_outline(tmp_path: Path) -> None:
    dsh_home = tmp_path / "dsh_home"
    _write_projcache(
        dsh_home,
        session_id="session-outline-only",
        cwd=str(tmp_path),
        created_at_ms=5000,
        title="Outline session",
        turns=[
            {"turn": 1, "prompt": "Turn 1 prompt", "response": "Turn 1 response"},
            {"turn": 2, "prompt": "Turn 2 prompt", "response": "Turn 2 response"},
        ],
    )

    res = read_dsh_session(dsh_home, session_id="session-outline-only", max_turns=1)
    assert res["status"] == "success"
    assert res["detail_level"] == "projcache_outline"
    assert res["total_turns"] == 2
    assert res["returned_turns"] == 1
    assert res["turns"][0]["user_messages"] == ["Turn 2 prompt"]
    assert res["turns"][0]["assistant_messages"] == ["Turn 2 response"]


def test_read_dsh_session_parses_full_v4_jsonl_and_filters_injected_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsh_home = tmp_path / "dsh_home"
    session_id = "session-full-001"
    _write_projcache(
        dsh_home,
        session_id=session_id,
        cwd=str(tmp_path),
        created_at_ms=8000,
        title="Checklist run",
        turns=[{"turn": 1, "prompt": "short", "response": "short"}],
    )

    session_file = dsh_home / "sessions" / "--encoded--" / session_id / "session.v4.jsonl.zstd"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"mock-zstd")

    fake_jsonl = "\n".join(
        [
            json.dumps({"type": "session", "version": 4, "id": session_id, "createdAt": 8000, "cwd": str(tmp_path)}),
            json.dumps({"type": "turn/start", "seq": 1, "data": {"turn": 1}}),
            json.dumps({"type": "user/message", "seq": 2, "data": {"content": [{"type": "text", "text": "Inspect scripts/"}]}}),
            json.dumps(
                {
                    "type": "user/message",
                    "seq": 3,
                    "data": {"content": [{"type": "text", "text": "<system-reminder>\nIgnore me\n</system-reminder>"}]},
                }
            ),
            json.dumps(
                {
                    "type": "user/message",
                    "seq": 4,
                    "data": {"content": [{"type": "text", "text": "Current runtime context. Policy snapshot."}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant/message",
                    "seq": 5,
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "reasoning", "text": "internal"},
                                {"type": "text", "text": "Reading files first."},
                                {"type": "tool-call", "id": "c1", "name": "read", "arguments": "{}"},
                            ],
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant/message",
                    "seq": 6,
                    "data": {
                        "turn": 1,
                        "step": 2,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool-call", "id": "c2", "name": "write", "arguments": "{}"},
                                {"type": "tool-call", "id": "c3", "name": "read", "arguments": "{}"},
                                {"type": "text", "text": "Checklist written cleanly."},
                            ],
                        },
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr(dsh_sessions, "_decompress_zstd_file", lambda _p: fake_jsonl)

    res = read_dsh_session(dsh_home, session_id=session_id)
    assert res["status"] == "success"
    assert res["detail_level"] == "full_jsonl"
    assert res["total_turns"] == 1
    turn = res["turns"][0]
    assert turn["user_messages"] == ["Inspect scripts/"]
    assert turn["assistant_messages"] == ["Reading files first.", "Checklist written cleanly."]
    assert turn["tools_called"] == ["read", "write"]


def test_read_dsh_session_rejects_path_traversal(tmp_path: Path) -> None:
    for bad_id in ["../secret", "nested/id", r"nested\id", "..", ""]:
        res = read_dsh_session(tmp_path, session_id=bad_id)
        assert res["status"] == "failed"
        assert "Invalid session_id" in res["summary"]


def test_find_zstd_executable_fallback_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_zstd = tmp_path / "zstd.exe"
    fake_zstd.write_text("bin", encoding="utf-8")

    monkeypatch.setenv("DSH_ZSTD_BIN", str(fake_zstd))
    monkeypatch.setattr(dsh_sessions.shutil, "which", lambda _name: None)
    assert dsh_sessions._find_zstd_executable() == str(fake_zstd)

    monkeypatch.delenv("DSH_ZSTD_BIN", raising=False)
    conda_zstd = tmp_path / "anaconda3" / "Library" / "bin" / "zstd.exe"
    conda_zstd.parent.mkdir(parents=True, exist_ok=True)
    conda_zstd.write_text("bin", encoding="utf-8")

    orig_path_cls = dsh_sessions.Path

    class _PatchedPath(type(Path())):
        @classmethod
        def home(cls) -> Path:
            return tmp_path

    monkeypatch.setattr(dsh_sessions.os, "name", "nt")
    monkeypatch.setattr(dsh_sessions, "Path", _PatchedPath)
    monkeypatch.setattr(
        _PatchedPath,
        "is_file",
        lambda self: str(self) == str(conda_zstd) and orig_path_cls(self).is_file(),
    )
    assert dsh_sessions._find_zstd_executable() == str(conda_zstd)
