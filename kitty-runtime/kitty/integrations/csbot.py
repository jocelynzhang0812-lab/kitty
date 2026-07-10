from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kitty.agent.loop import AgentRunResult, WireEmitter
from kitty.core.context import AgentRecord
from kitty.core.events import WireType


AgentFactory = Callable[[], Awaitable[Any]]


class CSBotTurnHandler:
    """Runs the repository's hardened CSAgent behind Kitty workers."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        agent_factory: AgentFactory | None = None,
        stream_chunk_size: int = 80,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.agent_factory = agent_factory
        self.stream_chunk_size = stream_chunk_size
        self._agent: Any = None
        self._lock = asyncio.Lock()
        self.document_count = 0

    async def startup(self) -> None:
        await self._get_agent()

    async def shutdown(self) -> None:
        if self._agent is None:
            return
        client = getattr(getattr(self._agent, "llm", None), "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            value = close()
            if inspect.isawaitable(value):
                await value
        database = getattr(self._agent, "_db_conn", None)
        close_database = getattr(database, "close", None)
        if callable(close_database):
            close_database()

    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        emit_wire: WireEmitter,
        *,
        session_id: str = "",
        record: AgentRecord | None = None,
    ) -> AgentRunResult:
        agent = await self._get_agent()
        user_id = record.effective_user_id() if record is not None else ""
        user_id = user_id or "anonymous"

        # Restore the CSAgent's in-memory conversation after a process restart
        # from Kitty's durable session history.
        session = agent.sessions.get_or_create(session_id)
        if not session.get("history") and history:
            session["history"] = [
                dict(message)
                for message in history[-20:]
                if message.get("role") in {"user", "assistant"}
            ]
            agent.sessions.set(session_id, session)

        mentioned = True
        if record is not None and record.meta is not None:
            mentioned = bool(record.meta.extra.get("mentioned", True))
        reply = await agent.handle_message(user_id, session_id, user_message, mentioned=mentioned)
        reply = str(reply or "")
        for offset in range(0, len(reply), self.stream_chunk_size):
            await emit_wire(
                WireType.TEXT_PART,
                {"text": reply[offset : offset + self.stream_chunk_size]},
            )
        return AgentRunResult(
            reply=reply,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ],
            steps=1,
        )

    async def _get_agent(self):
        if self._agent is not None:
            return self._agent
        async with self._lock:
            if self._agent is not None:
                return self._agent
            if self.agent_factory is not None:
                self._agent = await self.agent_factory()
            else:
                self._agent = await self._bootstrap_repository_agent()
            self._validate_agent(self._agent)
            return self._agent

    async def _bootstrap_repository_agent(self):
        main_path = self.project_root / "main.py"
        if not main_path.is_file():
            raise RuntimeError(f"CS-bot main.py not found: {main_path}")
        root_text = str(self.project_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        digest = hashlib.sha256(str(main_path).encode("utf-8")).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(f"kitty_csbot_main_{digest}", main_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load CS-bot bootstrap: {main_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return await module.bootstrap(project_root=self.project_root)

    def _validate_agent(self, agent: Any) -> None:
        if not callable(getattr(agent, "handle_message", None)):
            raise RuntimeError("CS-bot bootstrap did not return a compatible agent")
        if self.agent_factory is not None:
            return
        try:
            from csbot.agent.core import ToolRegistry

            kb = ToolRegistry.get_instance("search_knowledge_base")
            if kb is None:
                raise RuntimeError("CS-bot knowledge search tool is not registered")
            self.document_count = len(kb.index.all())
            if self.document_count == 0:
                raise RuntimeError("CS-bot knowledge index is empty")
        except ImportError:
            raise
