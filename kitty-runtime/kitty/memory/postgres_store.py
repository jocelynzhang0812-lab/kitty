from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from kitty.memory.session_store import SessionState


class PostgresUnavailableError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


_session_write_lease: ContextVar[tuple[str, str, int] | None] = ContextVar(
    "kitty_session_write_lease",
    default=None,
)


@dataclass(slots=True, frozen=True)
class InboxJob:
    job_id: str
    session_id: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str
    lease_expires_at: float


@dataclass(slots=True, frozen=True)
class OutboxJob:
    job_id: str
    inbox_job_id: str
    chat_id: str
    reply_text: str
    attempts: int
    lease_owner: str
    lease_expires_at: float


class PostgresStore:
    """Shared session, inbox, lease, and outbox store for distributed Kitty."""

    def __init__(
        self,
        database_url: str,
        *,
        ensure_schema: bool = True,
        max_pool_size: int = 10,
    ):
        if not database_url.strip():
            raise ValueError("KITTY_DATABASE_URL is required")
        self.database_url = database_url
        self._dict_row, self._jsonb, pool_type = _load_psycopg()
        self._pool = pool_type(
            conninfo=database_url,
            min_size=1,
            max_size=max(2, max_pool_size),
            kwargs={"row_factory": self._dict_row},
            open=True,
        )
        self._pool.wait(timeout=15)
        if ensure_schema:
            self.ensure_schema()

    def _connect(self):
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()

    def ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS kitty_sessions (
                session_id TEXT PRIMARY KEY,
                messages_json JSONB NOT NULL,
                metadata_json JSONB NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS kitty_events (
                event_id TEXT PRIMARY KEY,
                seen_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS kitty_inbox_jobs (
                job_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                payload_json JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at DOUBLE PRECISION NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_kitty_inbox_claim
            ON kitty_inbox_jobs(status, available_at, lease_expires_at, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS kitty_session_leases (
                session_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                fencing_token BIGINT NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS kitty_outbox_jobs (
                job_id TEXT PRIMARY KEY,
                inbox_job_id TEXT NOT NULL UNIQUE,
                chat_id TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at DOUBLE PRECISION NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_kitty_outbox_claim
            ON kitty_outbox_jobs(status, available_at, lease_expires_at, created_at)
            """,
        )
        with self._connect() as connection:
            for statement in statements:
                connection.execute(statement)

    def ping(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)

    def load(self, session_id: str) -> SessionState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitty_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            return SessionState(session_id=session_id)
        return SessionState(
            session_id=str(row["session_id"]),
            messages=list(row["messages_json"]),
            metadata=dict(row["metadata_json"]),
            updated_at=float(row["updated_at"]),
        )

    def save(self, state: SessionState) -> None:
        state.updated_at = time.time()
        with self._connect() as connection:
            lease = _session_write_lease.get()
            if lease is not None:
                session_id, owner, fencing_token = lease
                if session_id != state.session_id:
                    raise LeaseLostError("session write does not match active lease")
                active = connection.execute(
                    """
                    SELECT 1 FROM kitty_session_leases
                    WHERE session_id = %s AND owner = %s AND fencing_token = %s
                        AND expires_at > %s
                    FOR UPDATE
                    """,
                    (session_id, owner, fencing_token, time.time()),
                ).fetchone()
                if active is None:
                    raise LeaseLostError(f"session lease lost: {session_id}")
            connection.execute(
                """
                INSERT INTO kitty_sessions(session_id, messages_json, metadata_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages_json = EXCLUDED.messages_json,
                    metadata_json = EXCLUDED.metadata_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    state.session_id,
                    self._jsonb(state.messages),
                    self._jsonb(state.metadata),
                    state.updated_at,
                ),
            )

    @contextmanager
    def session_write_lease(self, session_id: str, owner: str, fencing_token: int):
        token = _session_write_lease.set((session_id, owner, fencing_token))
        try:
            yield
        finally:
            _session_write_lease.reset(token)

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM kitty_sessions WHERE session_id = %s", (session_id,))

    def list_session_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM kitty_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def accept_event(self, event_id: str, ttl_seconds: float = 86_400) -> bool:
        if not event_id:
            return True
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM kitty_events WHERE seen_at < %s", (now - ttl_seconds,))
            cursor = connection.execute(
                """
                INSERT INTO kitty_events(event_id, seen_at) VALUES (%s, %s)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, now),
            )
            return cursor.rowcount == 1

    def enqueue_inbox(self, job_id: str, session_id: str, payload: dict[str, Any]) -> bool:
        if not job_id or not session_id:
            raise ValueError("job_id and session_id are required")
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO kitty_inbox_jobs(
                    job_id, session_id, payload_json, available_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, session_id, self._jsonb(payload), now, now, now),
            )
            return cursor.rowcount == 1

    def claim_inbox(self, owner: str, lease_seconds: float) -> InboxJob | None:
        now = time.time()
        expires = now + lease_seconds
        with self._connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT jobs.job_id
                    FROM kitty_inbox_jobs AS jobs
                    WHERE ((
                        jobs.status = 'pending' AND jobs.available_at <= %s
                    ) OR (
                        jobs.status = 'processing' AND jobs.lease_expires_at <= %s
                    ))
                    AND NOT EXISTS (
                        SELECT 1 FROM kitty_session_leases AS leases
                        WHERE leases.session_id = jobs.session_id
                            AND leases.expires_at > %s
                    )
                    ORDER BY jobs.created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE kitty_inbox_jobs AS jobs
                SET status = 'processing', attempts = jobs.attempts + 1,
                    lease_owner = %s, lease_expires_at = %s, updated_at = %s
                FROM candidate
                WHERE jobs.job_id = candidate.job_id
                RETURNING jobs.*
                """,
                (now, now, now, owner, expires, now),
            ).fetchone()
        return _inbox_from_row(row) if row else None

    def get_inbox(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitty_inbox_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def renew_inbox_lease(self, job_id: str, owner: str, lease_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_inbox_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now + lease_seconds, now, job_id, owner),
            )
            return cursor.rowcount == 1

    def retry_inbox(self, job_id: str, owner: str, error: str, delay_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_inbox_jobs
                SET status = 'pending', available_at = %s, lease_owner = '',
                    lease_expires_at = 0, last_error = %s, updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now + max(0.0, delay_seconds), error[:2000], now, job_id, owner),
            )
            return cursor.rowcount == 1

    def defer_inbox(self, job_id: str, owner: str, delay_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_inbox_jobs
                SET status = 'pending', attempts = GREATEST(attempts - 1, 0),
                    available_at = %s, lease_owner = '', lease_expires_at = 0,
                    updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now + max(0.0, delay_seconds), now, job_id, owner),
            )
            return cursor.rowcount == 1

    def fail_inbox(self, job_id: str, owner: str, error: str) -> bool:
        return self._finish_failed("kitty_inbox_jobs", job_id, owner, error)

    def acquire_session_lease(
        self,
        session_id: str,
        owner: str,
        lease_seconds: float,
    ) -> int | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO kitty_session_leases(
                    session_id, owner, fencing_token, expires_at, updated_at
                ) VALUES (%s, %s, 1, %s, %s)
                ON CONFLICT(session_id) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    fencing_token = kitty_session_leases.fencing_token + 1,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                WHERE kitty_session_leases.expires_at <= %s
                RETURNING fencing_token
                """,
                (session_id, owner, now + lease_seconds, now, now),
            ).fetchone()
        return int(row["fencing_token"]) if row else None

    def renew_session_lease(
        self,
        session_id: str,
        owner: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_session_leases
                SET expires_at = %s, updated_at = %s
                WHERE session_id = %s AND owner = %s AND fencing_token = %s
                """,
                (now + lease_seconds, now, session_id, owner, fencing_token),
            )
            return cursor.rowcount == 1

    def release_session_lease(self, session_id: str, owner: str, fencing_token: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM kitty_session_leases
                WHERE session_id = %s AND owner = %s AND fencing_token = %s
                """,
                (session_id, owner, fencing_token),
            )
            return cursor.rowcount == 1

    def complete_inbox_with_outbox(
        self,
        job: InboxJob,
        owner: str,
        fencing_token: int,
        reply_text: str,
    ) -> str:
        now = time.time()
        outbox_id = f"outbox:{job.job_id}"
        chat_id = str(job.payload.get("chat_id") or "")
        if not chat_id:
            raise ValueError("inbox job has no chat_id")
        with self._connect() as connection:
            lease = connection.execute(
                """
                SELECT 1 FROM kitty_session_leases
                WHERE session_id = %s AND owner = %s AND fencing_token = %s
                    AND expires_at > %s
                FOR UPDATE
                """,
                (job.session_id, owner, fencing_token, now),
            ).fetchone()
            if lease is None:
                raise LeaseLostError(f"session lease lost: {job.session_id}")
            cursor = connection.execute(
                """
                UPDATE kitty_inbox_jobs
                SET status = 'completed', lease_owner = '', lease_expires_at = 0,
                    last_error = '', updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now, job.job_id, owner),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"inbox lease lost: {job.job_id}")
            connection.execute(
                """
                INSERT INTO kitty_outbox_jobs(
                    job_id, inbox_job_id, chat_id, reply_text,
                    available_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(inbox_job_id) DO NOTHING
                """,
                (outbox_id, job.job_id, chat_id, reply_text, now, now, now),
            )
        return outbox_id

    def claim_outbox(self, owner: str, lease_seconds: float) -> OutboxJob | None:
        now = time.time()
        expires = now + lease_seconds
        with self._connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT job_id
                    FROM kitty_outbox_jobs
                    WHERE (
                        status = 'pending' AND available_at <= %s
                    ) OR (
                        status = 'processing' AND lease_expires_at <= %s
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE kitty_outbox_jobs AS jobs
                SET status = 'processing', attempts = jobs.attempts + 1,
                    lease_owner = %s, lease_expires_at = %s, updated_at = %s
                FROM candidate
                WHERE jobs.job_id = candidate.job_id
                RETURNING jobs.*
                """,
                (now, now, owner, expires, now),
            ).fetchone()
        return _outbox_from_row(row) if row else None

    def get_outbox(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitty_outbox_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def clear_all(self) -> None:
        """Delete runtime data. Intended for isolated tests and local resets."""
        with self._connect() as connection:
            connection.execute(
                """
                TRUNCATE kitty_outbox_jobs, kitty_inbox_jobs,
                    kitty_session_leases, kitty_events, kitty_sessions
                """
            )

    def renew_outbox_lease(self, job_id: str, owner: str, lease_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_outbox_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now + lease_seconds, now, job_id, owner),
            )
            return cursor.rowcount == 1

    def complete_outbox(self, job_id: str, owner: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_outbox_jobs
                SET status = 'completed', lease_owner = '', lease_expires_at = 0,
                    last_error = '', updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now, job_id, owner),
            )
            return cursor.rowcount == 1

    def retry_outbox(self, job_id: str, owner: str, error: str, delay_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE kitty_outbox_jobs
                SET status = 'pending', available_at = %s, lease_owner = '',
                    lease_expires_at = 0, last_error = %s, updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (now + max(0.0, delay_seconds), error[:2000], now, job_id, owner),
            )
            return cursor.rowcount == 1

    def fail_outbox(self, job_id: str, owner: str, error: str) -> bool:
        return self._finish_failed("kitty_outbox_jobs", job_id, owner, error)

    def job_counts(self) -> dict[str, dict[str, int]]:
        return {
            "inbox": self._counts("kitty_inbox_jobs"),
            "outbox": self._counts("kitty_outbox_jobs"),
        }

    def requeue_dead(self, kind: str, job_id: str) -> bool:
        table = {
            "inbox": "kitty_inbox_jobs",
            "outbox": "kitty_outbox_jobs",
        }.get(kind)
        if table is None:
            raise ValueError("kind must be inbox or outbox")
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE {table}
                SET status = 'pending', attempts = 0, available_at = %s,
                    lease_owner = '', lease_expires_at = 0,
                    last_error = '', updated_at = %s
                WHERE job_id = %s AND status = 'dead'
                """,
                (now, now, job_id),
            )
            return cursor.rowcount == 1

    def _counts(self, table: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
            ).fetchall()
        result = {"pending": 0, "processing": 0, "completed": 0, "dead": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def _finish_failed(self, table: str, job_id: str, owner: str, error: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE {table}
                SET status = 'dead', lease_owner = '', lease_expires_at = 0,
                    last_error = %s, updated_at = %s
                WHERE job_id = %s AND status = 'processing' AND lease_owner = %s
                """,
                (error[:2000], now, job_id, owner),
            )
            return cursor.rowcount == 1


def _load_psycopg():
    try:
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise PostgresUnavailableError(
            "PostgreSQL mode requires psycopg; install kitty-feishu-runtime[postgres]"
        ) from exc
    return dict_row, Jsonb, ConnectionPool


def _inbox_from_row(row: dict[str, Any]) -> InboxJob:
    return InboxJob(
        job_id=str(row["job_id"]),
        session_id=str(row["session_id"]),
        payload=dict(row["payload_json"]),
        attempts=int(row["attempts"]),
        lease_owner=str(row["lease_owner"]),
        lease_expires_at=float(row["lease_expires_at"]),
    )


def _outbox_from_row(row: dict[str, Any]) -> OutboxJob:
    return OutboxJob(
        job_id=str(row["job_id"]),
        inbox_job_id=str(row["inbox_job_id"]),
        chat_id=str(row["chat_id"]),
        reply_text=str(row["reply_text"]),
        attempts=int(row["attempts"]),
        lease_owner=str(row["lease_owner"]),
        lease_expires_at=float(row["lease_expires_at"]),
    )
