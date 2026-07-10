from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RecordMeta:
    """Metadata exposed through ``ctx.record.meta`` in existing hooks."""

    title: str = ""
    channel: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRecord:
    """Best-effort compatibility record for channel/user information."""

    user_id: str = ""
    from_user: str = ""
    sender: str = ""
    chat_id: str = ""
    meta: RecordMeta | None = None

    def effective_user_id(self) -> str:
        return self.user_id or self.from_user or self.sender


@dataclass(slots=True)
class HookContext:
    """Context passed as the second argument to a Kitty hook."""

    record: AgentRecord
    work_dir: Path
    session_id: str
    logger: logging.Logger
    services: dict[str, Any] = field(default_factory=dict)
