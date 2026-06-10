from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .context import ShellContext
from .models import CheckpointPayload


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    agent_session_ref TEXT NOT NULL,
    resume_command TEXT NOT NULL,
    title TEXT,
    goal TEXT,
    latest_summary TEXT,
    clarification_requested INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    git_root TEXT,
    project TEXT,
    branch TEXT,
    hostname TEXT,
    tmux_session TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'stopped')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(agent, agent_session_ref)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT,
    goal TEXT,
    summary TEXT NOT NULL,
    completed_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    next_actions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class DoctorMatch:
    session_id: str
    title: str | None
    project: str | None
    branch: str | None
    status: str
    latest_summary: str | None
    resume_command: str
    score: int
    reasons: list[str]
    reason_scores: list[tuple[int, str]]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Registry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def start_session(self, agent: str, agent_session_ref: str, context: ShellContext) -> str:
        resume_command = f"{agent} resume {agent_session_ref}" if agent == "codex" else agent_session_ref
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                (agent, agent_session_ref),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE sessions
                    SET path = ?, git_root = ?, project = ?, branch = ?, hostname = ?, tmux_session = ?,
                        status = 'active', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        context.path,
                        context.git_root,
                        context.project,
                        context.branch,
                        context.hostname,
                        context.tmux_session,
                        now,
                        existing["id"],
                    ),
                )
                return str(existing["id"])

            session_id = f"asm_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO sessions (
                    id, agent, agent_session_ref, resume_command, title, goal, latest_summary, clarification_requested, path,
                    git_root, project, branch, hostname, tmux_session, status, started_at, ended_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?)
                """,
                (
                    session_id,
                    agent,
                    agent_session_ref,
                    resume_command,
                    context.path,
                    context.git_root,
                    context.project,
                    context.branch,
                    context.hostname,
                    context.tmux_session,
                    now,
                    now,
                ),
            )
            return session_id

    def start_or_touch_session(self, agent: str, agent_session_ref: str, context: ShellContext) -> str:
        return self.start_session(agent=agent, agent_session_ref=agent_session_ref, context=context)

    def require_session(self, session_id: str) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown session: {session_id}")
        return row

    def add_checkpoint(self, session_id: str, kind: str, payload: CheckpointPayload) -> None:
        current = self.require_session(session_id)
        has_existing_checkpoint = self._checkpoint_count(session_id) > 0
        if not has_existing_checkpoint and (not payload.title or not payload.goal):
            raise ValueError("first checkpoint must include title and goal")

        created_at = utc_now()
        checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    id, session_id, kind, title, goal, summary, completed_json, blockers_json,
                    next_actions_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    kind,
                    payload.title,
                    payload.goal,
                    payload.summary,
                    json.dumps(payload.completed),
                    json.dumps(payload.blockers),
                    json.dumps(payload.next_actions),
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET title = COALESCE(?, title),
                    goal = COALESCE(?, goal),
                    latest_summary = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.title,
                    payload.goal,
                    payload.summary,
                    created_at,
                    session_id,
                ),
            )

    def stop_session(self, session_id: str) -> None:
        self.require_session(session_id)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'stopped', ended_at = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )

    def touch_session_by_agent_ref(self, agent: str, agent_session_ref: str) -> str:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                (agent, agent_session_ref),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown session for {agent}:{agent_session_ref}")
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        return str(row["id"])

    def stop_session_by_agent_ref(self, agent: str, agent_session_ref: str) -> str:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                (agent, agent_session_ref),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown session for {agent}:{agent_session_ref}")
            conn.execute(
                "UPDATE sessions SET status = 'stopped', ended_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        return str(row["id"])

    def session_has_checkpoints(self, session_id: str) -> bool:
        return self._checkpoint_count(session_id) > 0

    def session_row_by_agent_ref(self, agent: str, agent_session_ref: str) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                (agent, agent_session_ref),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown session for {agent}:{agent_session_ref}")
        return row

    def update_session_semantics(self, session_id: str, *, title: str | None, goal: str | None) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = COALESCE(?, title),
                    goal = COALESCE(?, goal),
                    clarification_requested = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (title, goal, now, session_id),
            )

    def mark_clarification_requested(self, session_id: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET clarification_requested = 1, updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def list_sessions(
        self,
        limit: int = 20,
        *,
        agent: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        project: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[object] = []
        if agent:
            clauses.append("agent = ?")
            values.append(agent)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if branch:
            clauses.append("branch = ?")
            values.append(branch)
        if project:
            clauses.append("project = ?")
            values.append(project)

        where_sql = ""
        if clauses:
            where_sql = "WHERE " + " AND ".join(clauses)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, agent, agent_session_ref, resume_command, title, goal, project, branch, status,
                       updated_at, latest_summary
                FROM sessions
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*values, limit],
            ).fetchall()
        return list(rows)

    def doctor(self, context: ShellContext, limit: int = 5) -> list[DoctorMatch]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, project, branch, status, latest_summary, resume_command, path, git_root, updated_at
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()

        matches: list[DoctorMatch] = []
        for row in rows:
            score = 0
            reasons: list[str] = []
            reason_scores: list[tuple[int, str]] = []
            if row["path"] == context.path:
                score += 50
                reasons.append("same working directory")
                reason_scores.append((50, "same working directory"))
            if context.git_root and row["git_root"] == context.git_root:
                score += 30
                reasons.append("same git root")
                reason_scores.append((30, "same git root"))
            if context.branch and row["branch"] == context.branch:
                score += 20
                reasons.append("same branch")
                reason_scores.append((20, "same branch"))
            if row["status"] == "active":
                score += 10
                reasons.append("active session")
                reason_scores.append((10, "active session"))
            if score == 0:
                continue
            matches.append(
                DoctorMatch(
                    session_id=str(row["id"]),
                    title=row["title"],
                    project=row["project"],
                    branch=row["branch"],
                    status=row["status"],
                    latest_summary=row["latest_summary"],
                    resume_command=row["resume_command"],
                    score=score,
                    reasons=reasons,
                    reason_scores=reason_scores,
                )
            )
        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:limit]

    def _checkpoint_count(self, session_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["count"])
