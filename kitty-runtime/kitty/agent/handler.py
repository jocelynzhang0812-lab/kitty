from __future__ import annotations

from typing import Any, Protocol

from kitty.agent.loop import AgentRunResult, WireEmitter
from kitty.core.context import AgentRecord


class TurnHandler(Protocol):
    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        emit_wire: WireEmitter,
        *,
        session_id: str = "",
        record: AgentRecord | None = None,
    ) -> AgentRunResult: ...


class LifecycleHandler(TurnHandler, Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...
