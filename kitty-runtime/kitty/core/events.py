from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class EventType(StrEnum):
    """Runtime event names inferred from the CS-bot hook contract."""

    CLI_WIRE = "cli.wire"
    CLI_TURN_DONE = "cli.turn_done"
    WORKER_STARTED = "worker.started"
    WORKER_FAILED = "worker.failed"
    WORKER_STOPPED = "worker.stopped"


class WireType(StrEnum):
    """Events emitted while one model turn is running."""

    TURN_BEGIN = "TurnBegin"
    TEXT_PART = "TextPart"
    CONTENT_PART = "ContentPart"
    TOOL_CALL = "ToolCall"
    TOOL_RESULT = "ToolResult"
    TURN_END = "TurnEnd"


def _event_name(value: str | EventType) -> str:
    return value.value if isinstance(value, EventType) else str(value)


def _wire_name(value: str | WireType) -> str:
    return value.value if isinstance(value, WireType) else str(value)


@dataclass(slots=True, frozen=True)
class SessionEvent:
    """Serializable event passed to Kitty-compatible hooks."""

    event_type: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def create(
        cls,
        event_type: str | EventType,
        session_id: str,
        data: Mapping[str, Any] | None = None,
    ) -> "SessionEvent":
        return cls(
            event_type=_event_name(event_type),
            session_id=session_id,
            data=dict(data or {}),
        )

    @classmethod
    def wire(
        cls,
        session_id: str,
        wire_type: str | WireType,
        **payload: Any,
    ) -> "SessionEvent":
        wire = {"wire_type": _wire_name(wire_type), **payload}
        return cls.create(EventType.CLI_WIRE, session_id, {"wire": wire})

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionEvent":
        return cls(
            event_id=str(value.get("event_id") or uuid.uuid4().hex),
            event_type=str(value["event_type"]),
            session_id=str(value["session_id"]),
            timestamp=float(value.get("timestamp") or time.time()),
            data=dict(value.get("data") or {}),
        )
