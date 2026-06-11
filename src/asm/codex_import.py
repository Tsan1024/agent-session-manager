from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .text import condense_message


@dataclass(frozen=True)
class ImportedCodexSession:
    agent_session_ref: str
    path: str
    started_at: str
    updated_at: str
    initial_prompt: str | None
    latest_summary: str | None
    session_file_path: str
    session_file_mtime_ns: int


def iter_codex_session_files(codex_home: Path) -> list[Path]:
    sessions_root = codex_home.expanduser() / "sessions"
    if not sessions_root.exists():
        return []
    return sorted(sessions_root.rglob("*.jsonl"))


def importable_codex_sessions(
    codex_home: Path,
    *,
    limit: int | None = None,
    known_mtimes: dict[str, int] | None = None,
) -> list[ImportedCodexSession]:
    files = iter_codex_session_files(codex_home)
    if limit is not None:
        files = files[-limit:]

    imported: list[ImportedCodexSession] = []
    for session_file in files:
        if known_mtimes is not None:
            try:
                current_mtime_ns = session_file.stat().st_mtime_ns
            except OSError:
                continue
            if known_mtimes.get(str(session_file)) == current_mtime_ns:
                continue
        parsed = parse_codex_session_file(session_file)
        if parsed is not None:
            imported.append(parsed)
    return imported


def parse_codex_session_file(session_file: Path) -> ImportedCodexSession | None:
    meta_id: str | None = None
    started_at: str | None = None
    cwd: str | None = None
    updated_at: str | None = None
    first_user_prompt: str | None = None
    last_assistant_message: str | None = None

    try:
        stat = session_file.stat()
        raw_lines = session_file.read_text().splitlines()
    except OSError:
        return None

    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        timestamp = item.get("timestamp")
        if isinstance(timestamp, str):
            updated_at = timestamp

        item_type = item.get("type")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue

        if item_type == "session_meta":
            meta_id = _as_string(payload.get("id")) or meta_id
            started_at = _as_string(payload.get("timestamp")) or started_at
            cwd = _as_string(payload.get("cwd")) or cwd
            continue

        if item_type != "response_item":
            continue

        if payload.get("type") != "message":
            continue

        role = payload.get("role")
        content = payload.get("content")
        text = _extract_message_text(content)
        if not text:
            continue
        if role == "user" and first_user_prompt is None and not _is_non_task_user_payload(text):
            first_user_prompt = condense_message(text, limit=100)
        if role == "assistant":
            condensed = condense_message(text, limit=100)
            if condensed:
                last_assistant_message = condensed

    if not meta_id or not cwd or not started_at:
        return None

    final_updated_at = updated_at or started_at
    return ImportedCodexSession(
        agent_session_ref=meta_id,
        path=cwd,
        started_at=started_at,
        updated_at=final_updated_at,
        initial_prompt=first_user_prompt,
        latest_summary=last_assistant_message,
        session_file_path=str(session_file),
        session_file_mtime_ns=stat.st_mtime_ns,
    )


def _extract_message_text(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    if not chunks:
        return None
    return "\n".join(chunks)


def _is_non_task_user_payload(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def _as_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
