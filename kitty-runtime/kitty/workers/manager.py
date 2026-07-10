from __future__ import annotations

import asyncio
from pathlib import Path

from kitty.agent.loop import AgentLoop
from kitty.core.context import AgentRecord
from kitty.hooks.bus import HookBus
from kitty.memory.session_store import SQLiteSessionStore
from kitty.workers.worker import SessionWorker, WorkerResult


class WorkerManager:
    def __init__(
        self,
        *,
        agent: AgentLoop,
        hooks: HookBus,
        store: SQLiteSessionStore,
        workspace_root: Path,
        log_dir: Path,
    ):
        self.agent = agent
        self.hooks = hooks
        self.store = store
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self._workers: dict[str, SessionWorker] = {}
        self._lock = asyncio.Lock()

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._workers)

    async def get_or_create(self, session_id: str) -> SessionWorker:
        if not session_id.strip():
            raise ValueError("session_id cannot be empty")
        async with self._lock:
            worker = self._workers.get(session_id)
            if worker is None:
                worker = SessionWorker(
                    session_id,
                    agent=self.agent,
                    hooks=self.hooks,
                    store=self.store,
                    workspace_root=self.workspace_root,
                    log_dir=self.log_dir,
                )
                self._workers[session_id] = worker
            return worker

    async def dispatch(
        self,
        session_id: str,
        message: str,
        *,
        record: AgentRecord | None = None,
        request_id: str | None = None,
    ) -> WorkerResult:
        worker = await self.get_or_create(session_id)
        return await worker.dispatch(message, record=record, request_id=request_id)

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            worker = self._workers.pop(session_id, None)
        if worker is not None:
            await worker.stop()

    async def shutdown(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        await asyncio.gather(*(worker.stop() for worker in workers))
