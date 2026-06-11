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
    initial_prompt TEXT,
    goal TEXT,
    latest_summary TEXT,
    latest_summary_source TEXT CHECK(latest_summary_source IN ('agent', 'fallback')),
    clarification_requested INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    git_root TEXT,
    project TEXT,
    branch TEXT,
    hostname TEXT,
    tmux_session TEXT,
    session_file_path TEXT,
    session_file_mtime_ns INTEGER,
    imported_at TEXT,
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
    source TEXT NOT NULL CHECK(source IN ('agent', 'fallback')),
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
    initial_prompt: str | None
    project: str | None
    path: str | None
    branch: str | None
    status: str
    latest_summary: str | None
    latest_summary_source: str | None
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
            self._migrate(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "clarification_requested" not in session_columns:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN clarification_requested INTEGER NOT NULL DEFAULT 0"
            )
        if "initial_prompt" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN initial_prompt TEXT")
        if "latest_summary_source" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN latest_summary_source TEXT")
        if "session_file_path" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN session_file_path TEXT")
        if "session_file_mtime_ns" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN session_file_mtime_ns INTEGER")
        if "imported_at" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN imported_at TEXT")

        checkpoint_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(checkpoints)").fetchall()
        }
        if "source" not in checkpoint_columns:
            conn.execute(
                "ALTER TABLE checkpoints ADD COLUMN source TEXT NOT NULL DEFAULT 'agent' CHECK(source IN ('agent', 'fallback'))"
            )

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
                    id, agent, agent_session_ref, resume_command, title, initial_prompt, goal, latest_summary, clarification_requested, path,
                    git_root, project, branch, hostname, tmux_session, session_file_path, session_file_mtime_ns, imported_at, status, started_at, ended_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'active', ?, NULL, ?)
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

    def add_checkpoint(
        self,
        session_id: str,
        kind: str,
        payload: CheckpointPayload,
        *,
        source: str = "agent",
    ) -> None:
        current = self.require_session(session_id)
        has_existing_checkpoint = self._checkpoint_count(session_id) > 0
        has_session_semantics = bool(current["title"] and current["goal"])
        requires_semantics = kind != "final"
        if requires_semantics and not has_existing_checkpoint and not has_session_semantics and (
            not payload.title or not payload.goal
        ):
            raise ValueError("first checkpoint must include title and goal")

        created_at = utc_now()
        checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    id, session_id, kind, source, title, goal, summary, completed_json, blockers_json,
                    next_actions_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    kind,
                    source,
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
                    latest_summary_source = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.title,
                    payload.goal,
                    payload.summary,
                    source,
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

    def record_initial_prompt(self, session_id: str, prompt: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET initial_prompt = COALESCE(initial_prompt, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (prompt, now, session_id),
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
                SELECT id, agent, agent_session_ref, resume_command, title, initial_prompt, goal, project, branch, status,
                       updated_at, latest_summary, latest_summary_source, path
                FROM sessions
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*values, limit],
            ).fetchall()
        return list(rows)

    def search_sessions(
        self,
        query: str,
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

        normalized_query = f"%{query.strip()}%"
        clauses.append(
            "("
            "COALESCE(title, '') LIKE ? OR "
            "COALESCE(initial_prompt, '') LIKE ? OR "
            "COALESCE(goal, '') LIKE ? OR "
            "COALESCE(latest_summary, '') LIKE ? OR "
            "COALESCE(project, '') LIKE ? OR "
            "COALESCE(branch, '') LIKE ? OR "
            "COALESCE(path, '') LIKE ?"
            ")"
        )
        values.extend([normalized_query] * 7)

        where_sql = "WHERE " + " AND ".join(clauses)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, agent, agent_session_ref, resume_command, title, initial_prompt, goal, project, branch, status,
                       updated_at, latest_summary, latest_summary_source, path
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
                SELECT id, title, initial_prompt, project, branch, status, latest_summary, latest_summary_source,
                       resume_command, path, git_root, updated_at
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
                    initial_prompt=row["initial_prompt"],
                    project=row["project"],
                    path=row["path"],
                    branch=row["branch"],
                    status=row["status"],
                    latest_summary=row["latest_summary"],
                    latest_summary_source=row["latest_summary_source"],
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

    def session_has_final_checkpoint(self, session_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM checkpoints WHERE session_id = ? AND kind = 'final'",
                (session_id,),
            ).fetchone()
        return int(row["count"]) > 0

    def imported_codex_file_mtimes(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT session_file_path, session_file_mtime_ns
                FROM sessions
                WHERE agent = 'codex' AND session_file_path IS NOT NULL AND session_file_mtime_ns IS NOT NULL
                """
            ).fetchall()
        return {
            str(row["session_file_path"]): int(row["session_file_mtime_ns"])
            for row in rows
            if row["session_file_path"] is not None and row["session_file_mtime_ns"] is not None
        }

    def import_codex_session(
        self,
        *,
        agent_session_ref: str,
        path: str,
        git_root: str | None,
        project: str | None,
        branch: str | None,
        started_at: str,
        updated_at: str,
        initial_prompt: str | None,
        latest_summary: str | None,
        session_file_path: str,
        session_file_mtime_ns: int,
        status: str,
    ) -> tuple[str, str]:
        now = utc_now()
        resume_command = f"codex resume {agent_session_ref}"
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", agent_session_ref),
            ).fetchone()
            if existing:
                latest_summary_value = existing["latest_summary"]
                latest_summary_source = existing["latest_summary_source"]
                if not latest_summary_value and latest_summary:
                    latest_summary_value = latest_summary

                conn.execute(
                    """
                    UPDATE sessions
                    SET resume_command = ?,
                        path = COALESCE(path, ?),
                        git_root = COALESCE(git_root, ?),
                        project = COALESCE(project, ?),
                        branch = COALESCE(branch, ?),
                        initial_prompt = COALESCE(initial_prompt, ?),
                        latest_summary = ?,
                        latest_summary_source = ?,
                        session_file_path = COALESCE(session_file_path, ?),
                        session_file_mtime_ns = ?,
                        imported_at = ?,
                        status = ?,
                        started_at = CASE
                            WHEN started_at IS NULL OR started_at = '' THEN ?
                            ELSE started_at
                        END,
                        updated_at = CASE
                            WHEN updated_at >= ? THEN updated_at
                            ELSE ?
                        END
                    WHERE id = ?
                    """,
                    (
                        resume_command,
                        path,
                        git_root,
                        project,
                        branch,
                        initial_prompt,
                        latest_summary_value,
                        latest_summary_source,
                        session_file_path,
                        session_file_mtime_ns,
                        now,
                        status,
                        started_at,
                        updated_at,
                        updated_at,
                        existing["id"],
                    ),
                )
                return str(existing["id"]), "updated"

            session_id = f"asm_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO sessions (
                    id, agent, agent_session_ref, resume_command, title, initial_prompt, goal, latest_summary, latest_summary_source,
                    clarification_requested, path, git_root, project, branch, hostname, tmux_session, session_file_path, session_file_mtime_ns, imported_at,
                    status, started_at, ended_at, updated_at
                )
                VALUES (?, 'codex', ?, ?, NULL, ?, NULL, ?, NULL, 0, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    session_id,
                    agent_session_ref,
                    resume_command,
                    initial_prompt,
                    latest_summary,
                    path,
                    git_root,
                    project,
                    branch,
                    session_file_path,
                    session_file_mtime_ns,
                    now,
                    status,
                    started_at,
                    updated_at,
                ),
            )
            return session_id, "created"
