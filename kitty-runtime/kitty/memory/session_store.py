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
