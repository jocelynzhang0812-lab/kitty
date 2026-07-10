from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kitty.core.context import AgentRecord


@dataclass(slots=True, frozen=True)
class ChannelMessage:
    session_id: str
    content: str
    record: AgentRecord = field(default_factory=AgentRecord)
    request_id: str | None = None


class ChannelAdapter(Protocol):
    async def run(self) -> None: ...

    async def send(self, message: ChannelMessage, reply: str) -> Any: ...
