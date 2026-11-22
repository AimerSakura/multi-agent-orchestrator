"""Read-only native DeepSeek Harness (`dsh`) session listing and history inspection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


def resolve_dsh_home(configured_home: str | None = None) -> Path:
    """Resolve the DSH state directory (`DSH_HOME` override or `~/.dsh`)."""
    if configured_home and configured_home.strip():
        return Path(configured_home.strip()).expanduser()
    return Path.home() / ".dsh"


def _normalize_dir_key(path_str: str) -> str:
    stripped = path_str.strip()
    if not stripped:
        return ""
    try:
        resolved = str(Path(stripped).expanduser().resolve())
    except OSError:
        resolved = os.path.normpath(stripped)
    return os.path.normcase(resolved).rstrip("\\/")


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n...[truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker


def _is_injected_dsh_user_text(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<system-reminder>") or stripped.startswith("Current runtime context.")


def _load_projcache_entry(proj_file: Path) -> dict[str, Any] | None:
    if not proj_file.is_file():
        return None
    try:
        raw = json.loads(proj_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None

    record = raw.get("record")
    if not isinstance(record, Mapping):
        return None
    identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
    rows = record.get("rows") if isinstance(record.get("rows"), Mapping) else {}

    title_val = (rows.get("title") or {}).get("val") if isinstance(rows.get("title"), Mapping) else ""
    outline_val = (rows.get("turnOutline") or {}).get("val") if isinstance(rows.get("turnOutline"), Mapping) else {}
    turns = outline_val.get("turns") if isinstance(outline_val, Mapping) else []
    model_val = (rows.get("modelSelection") or {}).get("val") if isinstance(rows.get("modelSelection"), Mapping) else None

    return {
        "session_id": proj_file.stem,
        "cwd": str(identity.get("cwd") or ""),
        "created_at_ms": identity.get("createdAt"),
        "title": title_val if isinstance(title_val, str) else "",
        "turn_outline": turns if isinstance(turns, list) else [],
        "model_selection": model_val,
    }


def list_dsh_sessions(
    dsh_home: Path,
    *,
    workspace_dir: str | None = None,
    limit: int = 20,
    search_term: str | None = None,
) -> dict[str, Any]:
    """List native DeepSeek Harness sessions from `session_projcache`."""
    clamped_limit = max(1, min(int(limit), 100))
    proj_dir = dsh_home / "storages" / "session_projcache" / "sessions"
    if not proj_dir.is_dir():
        return {"status": "success", "dsh_home": str(dsh_home), "count": 0, "sessions": []}

    target_dir_key = _normalize_dir_key(workspace_dir) if workspace_dir and workspace_dir.strip() else ""
    needle = search_term.strip().lower() if search_term and search_term.strip() else ""

    matched: list[tuple[tuple[int, float], dict[str, Any]]] = []
    for path in proj_dir.glob("*.json"):
        entry = _load_projcache_entry(path)
        if entry is None:
            continue
        if target_dir_key and _normalize_dir_key(entry["cwd"]) != target_dir_key:
            continue
        if needle:
            haystack = f"{entry['session_id']}\n{entry['title']}\n{entry['cwd']}".lower()
            if needle not in haystack:
                continue

        created_ms = entry["created_at_ms"]
        sort_ts = created_ms if isinstance(created_ms, (int, float)) else 0
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        turns = entry["turn_outline"]
        latest_prompt = ""
        if turns and isinstance(turns[-1], Mapping):
            raw_prompt = turns[-1].get("prompt")
            if isinstance(raw_prompt, str):
                latest_prompt = _truncate_text(raw_prompt.strip(), 200)

        matched.append(
            (
                (int(sort_ts), mtime),
                {
                    "session_id": entry["session_id"],
                    "title": entry["title"],
                    "cwd": entry["cwd"],
                    "created_at_ms": entry["created_at_ms"],
                    "turn_count": len(turns),
                    "latest_prompt": latest_prompt,
                    "model_selection": entry["model_selection"],
                },
            )
        )

    matched.sort(key=lambda item: item[0], reverse=True)
    sessions = [item[1] for item in matched[:clamped_limit]]
    return {
        "status": "success",
        "dsh_home": str(dsh_home),
        "count": len(sessions),
        "sessions": sessions,
    }


def _decompress_zstd_file(path: Path) -> str | None:
    """Decompress a `.zstd` file using optional `zstandard` package or local `zstd` CLI."""
    try:
        import zstandard  # type: ignore[import-not-found]

        dctx = zstandard.ZstdDecompressor()
        with path.open("rb") as handle:
            with dctx.stream_reader(handle) as reader:
                data = reader.read()
        return data.decode("utf-8", errors="replace")
    except ImportError:
        pass
    except Exception:
        return None

    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        return None

    try:
        proc = subprocess.run(
            [zstd_bin, "-dc", str(path)],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _extract_user_texts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") not in (None, "text"):
            continue
        raw = part.get("text")
        if isinstance(raw, str) and raw.strip() and not _is_injected_dsh_user_text(raw):
            texts.append(raw.strip())
    return texts


def _parse_dsh_v4_jsonl(
    jsonl_text: str,
    *,
    max_turns: int,
    max_chars_per_message: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Parse DSH `session.v4.jsonl` event stream into metadata and summarized turns."""
    session_meta: dict[str, Any] = {}
    turns_by_num: dict[int, dict[str, Any]] = {}
    turn_order: list[int] = []
    current_turn = 0

    def _ensure_turn(turn_num: int) -> dict[str, Any]:
        if turn_num not in turns_by_num:
            turns_by_num[turn_num] = {
                "turn": turn_num,
                "user_messages": [],
                "assistant_messages": [],
                "tools_called": [],
            }
            turn_order.append(turn_num)
        return turns_by_num[turn_num]

    for raw_line in jsonl_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue

        ev_type = event.get("type")
        data = event.get("data")

        if ev_type == "session":
            session_meta = {
                "cwd": str(event.get("cwd") or ""),
                "created_at_ms": event.get("createdAt"),
                "version": event.get("version"),
            }
        elif ev_type == "session/title" and isinstance(data, Mapping):
            title = data.get("title")
            if isinstance(title, str) and title.strip():
                session_meta["title"] = title.strip()
        elif ev_type == "turn/start" and isinstance(data, Mapping):
            turn_num = data.get("turn")
            if isinstance(turn_num, int) and not isinstance(turn_num, bool):
                current_turn = turn_num
                _ensure_turn(turn_num)
        elif ev_type == "user/message" and isinstance(data, Mapping):
            texts = _extract_user_texts(data.get("content"))
            if texts:
                bucket = _ensure_turn(current_turn or 1)
                bucket["user_messages"].append(_truncate_text("\n".join(texts), max_chars_per_message))
        elif ev_type == "assistant/message" and isinstance(data, Mapping):
            turn_num = data.get("turn")
            effective_turn = (
                turn_num
                if isinstance(turn_num, int) and not isinstance(turn_num, bool)
                else (current_turn or 1)
            )
            message = data.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            bucket = _ensure_turn(effective_turn)
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        bucket["assistant_messages"].append(_truncate_text(text.strip(), max_chars_per_message))
                elif part_type == "tool-call":
                    tool_name = part.get("name")
                    if isinstance(tool_name, str) and tool_name.strip() and tool_name not in bucket["tools_called"]:
                        bucket["tools_called"].append(tool_name.strip())

    all_turns = [turns_by_num[num] for num in turn_order]
    return session_meta, all_turns[-max_turns:], len(all_turns)


def read_dsh_session(
    dsh_home: Path,
    *,
    session_id: str,
    max_turns: int = 20,
    max_chars_per_message: int = 4000,
) -> dict[str, Any]:
    """Read a native DeepSeek Harness session from `.v4.jsonl.zstd` with `projcache` fallback."""
    clean_id = session_id.strip()
    if (
        not clean_id
        or clean_id in {".", ".."}
        or "/" in clean_id
        or "\\" in clean_id
        or ".." in clean_id
        or "\x00" in clean_id
    ):
        return {"status": "failed", "summary": "Invalid session_id."}

    clamped_turns = max(1, min(int(max_turns), 100))
    clamped_chars = max(200, min(int(max_chars_per_message), 20000))

    proj_file = dsh_home / "storages" / "session_projcache" / "sessions" / f"{clean_id}.json"
    proj_entry = _load_projcache_entry(proj_file)

    zstd_file: Path | None = None
    sessions_root = dsh_home / "sessions"
    if sessions_root.is_dir():
        matches = sorted(sessions_root.glob(f"*/{clean_id}/session.v4.jsonl.zstd"))
        if matches:
            zstd_file = matches[0]

    if proj_entry is None and zstd_file is None:
        return {"status": "failed", "summary": f"DSH session '{clean_id}' not found under {dsh_home}."}

    session_info: dict[str, Any] = {
        "session_id": clean_id,
        "title": proj_entry["title"] if proj_entry else "",
        "cwd": proj_entry["cwd"] if proj_entry else "",
        "created_at_ms": proj_entry["created_at_ms"] if proj_entry else None,
        "model_selection": proj_entry["model_selection"] if proj_entry else None,
        "session_file": str(zstd_file) if zstd_file else None,
    }

    if zstd_file is not None:
        raw_jsonl = _decompress_zstd_file(zstd_file)
        if raw_jsonl is not None:
            jsonl_meta, turns, total_turns = _parse_dsh_v4_jsonl(
                raw_jsonl,
                max_turns=clamped_turns,
                max_chars_per_message=clamped_chars,
            )
            if not session_info["cwd"] and jsonl_meta.get("cwd"):
                session_info["cwd"] = jsonl_meta["cwd"]
            if session_info["created_at_ms"] is None and jsonl_meta.get("created_at_ms") is not None:
                session_info["created_at_ms"] = jsonl_meta["created_at_ms"]
            if not session_info["title"] and jsonl_meta.get("title"):
                session_info["title"] = jsonl_meta["title"]
            return {
                "status": "success",
                "detail_level": "full_jsonl",
                "session": session_info,
                "total_turns": total_turns,
                "returned_turns": len(turns),
                "turns": turns,
            }

    raw_outline = proj_entry["turn_outline"] if proj_entry else []
    outline_turns: list[dict[str, Any]] = []
    for item in raw_outline[-clamped_turns:]:
        if not isinstance(item, Mapping):
            continue
        prompt_text = item.get("prompt")
        response_text = item.get("response")
        outline_turns.append(
            {
                "turn": item.get("turn"),
                "user_messages": [_truncate_text(prompt_text.strip(), clamped_chars)]
                if isinstance(prompt_text, str) and prompt_text.strip()
                else [],
                "assistant_messages": [_truncate_text(response_text.strip(), clamped_chars)]
                if isinstance(response_text, str) and response_text.strip()
                else [],
                "tools_called": [],
            }
        )

    return {
        "status": "success",
        "detail_level": "projcache_outline",
        "session": session_info,
        "total_turns": len(raw_outline),
        "returned_turns": len(outline_turns),
        "turns": outline_turns,
    }
