from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SessionState:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class FeishuDeliveryJob:
    job_id: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: float
    reply_text: str = ""
    reply_ready: bool = False
    last_error: str = ""


class SQLiteSessionStore:
    """Small durable store; each operation owns its SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kitty_sessions (
                    session_id TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kitty_events (
                    event_id TEXT PRIMARY KEY,
                    seen_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kitty_feishu_jobs (
                    job_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    reply_text TEXT NOT NULL DEFAULT '',
                    reply_ready INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kitty_feishu_jobs_pending
                ON kitty_feishu_jobs(status, available_at)
                """
            )

    def load(self, session_id: str) -> SessionState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitty_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return SessionState(session_id=session_id)
        return SessionState(
            session_id=row["session_id"],
            messages=json.loads(row["messages_json"]),
            metadata=json.loads(row["metadata_json"]),
            updated_at=float(row["updated_at"]),
        )

    def save(self, state: SessionState) -> None:
        state.updated_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO kitty_sessions(session_id, messages_json, metadata_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.session_id,
                    json.dumps(state.messages, ensure_ascii=False),
                    json.dumps(state.metadata, ensure_ascii=False),
                    state.updated_at,
                ),
            )

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM kitty_sessions WHERE session_id = ?", (session_id,))

    def list_session_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM kitty_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def accept_event(self, event_id: str, ttl_seconds: float = 86_400) -> bool:
        """Persistently deduplicate channel events across process restarts."""

        if not event_id:
            return True
        now = time.time()
        cutoff = now - ttl_seconds
        with self._connect() as connection:
            connection.execute("DELETE FROM kitty_events WHERE seen_at < ?", (cutoff,))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO kitty_events(event_id, seen_at) VALUES (?, ?)",
                (event_id, now),
            )
            return cursor.rowcount == 1

    def enqueue_feishu_job(self, job_id: str, payload: dict[str, Any]) -> bool:
        """Persist a Feishu delivery before acknowledging its webhook."""

        if not job_id:
            raise ValueError("Feishu delivery job_id is required")
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._connect() as connection:
            # Completed jobs only need to cover Feishu's retry window. Keeping
            # seven days gives operators time to inspect recent deliveries.
            connection.execute(
                "DELETE FROM kitty_feishu_jobs WHERE status = 'completed' AND updated_at < ?",
                (now - 7 * 86_400,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO kitty_feishu_jobs(
                    job_id, payload_json, status, attempts, available_at,
                    reply_text, reply_ready, last_error, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, '', 0, '', ?, ?)
                """,
                (job_id, encoded, now, now, now),
            )
            return cursor.rowcount == 1

    def recover_feishu_jobs(self) -> list[str]:
        """Recover interrupted jobs on the documented single-replica deployment."""

        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'pending', available_at = ?, updated_at = ?
                WHERE status = 'processing'
                """,
                (now, now),
            )
            rows = connection.execute(
                """
                SELECT job_id FROM kitty_feishu_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def load_feishu_job(self, job_id: str) -> FeishuDeliveryJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitty_feishu_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._delivery_job_from_row(row) if row is not None else None

    def claim_feishu_job(self, job_id: str) -> FeishuDeliveryJob | None:
        """Atomically move one due job from pending to processing."""

        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND status = 'pending' AND available_at <= ?
                """,
                (now, job_id, now),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM kitty_feishu_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._delivery_job_from_row(row) if row is not None else None

    def save_feishu_reply(self, job_id: str, reply_text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET reply_text = ?, reply_ready = 1, updated_at = ?
                WHERE job_id = ? AND status = 'processing'
                """,
                (reply_text, time.time(), job_id),
            )

    def retry_feishu_job(self, job_id: str, error: str, delay_seconds: float) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'pending', available_at = ?, last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'processing'
                """,
                (now + max(0.0, delay_seconds), error[:2000], now, job_id),
            )

    def complete_feishu_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'completed', last_error = '', updated_at = ?
                WHERE job_id = ? AND status = 'processing'
                """,
                (time.time(), job_id),
            )

    def fail_feishu_job(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'dead', last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'processing'
                """,
                (error[:2000], time.time(), job_id),
            )

    def requeue_dead_feishu_job(self, job_id: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_feishu_jobs
                SET status = 'pending', attempts = 0, available_at = ?,
                    last_error = '', updated_at = ?
                WHERE job_id = ? AND status = 'dead'
                """,
                (now, now, job_id),
            )
            return cursor.rowcount == 1

    def feishu_job_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM kitty_feishu_jobs GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "processing": 0, "completed": 0, "dead": 0}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return counts

    @staticmethod
    def _delivery_job_from_row(row: sqlite3.Row) -> FeishuDeliveryJob:
        return FeishuDeliveryJob(
            job_id=str(row["job_id"]),
            payload=json.loads(row["payload_json"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            available_at=float(row["available_at"]),
            reply_text=str(row["reply_text"]),
            reply_ready=bool(row["reply_ready"]),
            last_error=str(row["last_error"]),
        )
