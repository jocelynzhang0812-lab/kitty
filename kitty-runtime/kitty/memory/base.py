from __future__ import annotations

from typing import Protocol

from kitty.memory.session_store import SessionState


class SessionStore(Protocol):
    """Storage contract required by session workers."""

    def load(self, session_id: str) -> SessionState: ...

    def save(self, state: SessionState) -> None: ...

    def delete(self, session_id: str) -> None: ...

    def list_session_ids(self) -> list[str]: ...

    def accept_event(self, event_id: str, ttl_seconds: float = 86_400) -> bool: ...
