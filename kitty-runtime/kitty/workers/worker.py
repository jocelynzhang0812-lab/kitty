from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from kitty.agent.loop import AgentLoop
from kitty.core.context import AgentRecord, HookContext, RecordMeta
from kitty.core.events import EventType, SessionEvent, WireType
from kitty.hooks.bus import HookBus
from kitty.memory.session_store import SQLiteSessionStore


@dataclass(slots=True, frozen=True)
class WorkerResult:
    request_id: str
    session_id: str
    reply: str
    steps: int


@dataclass(slots=True)
class _WorkerRequest:
    request_id: str
    message: str
    record: AgentRecord
    future: asyncio.Future[WorkerResult]


_STOP = object()


def safe_session_name(session_id: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-.")[:32] or "session"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def build_worker_logger(session_id: str, log_dir: Path) -> logging.Logger:
    name = f"kitty.worker.{hashlib.sha256(session_id.encode()).hexdigest()[:16]}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(
            log_dir / f"worker_{safe_session_name(session_id)}.log",
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class SessionWorker:
    """Serializes turns for one session while other sessions run concurrently."""

    def __init__(
        self,
        session_id: str,
        *,
        agent: AgentLoop,
        hooks: HookBus,
        store: SQLiteSessionStore,
        workspace_root: Path,
        log_dir: Path,
        max_history_messages: int = 200,
    ):
        self.session_id = session_id
        self.agent = agent
        self.hooks = hooks
        self.store = store
        self.work_dir = workspace_root / safe_session_name(session_id)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.logger = build_worker_logger(session_id, log_dir)
        self.max_history_messages = max_history_messages
        self._queue: asyncio.Queue[_WorkerRequest | object] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._stopped = False
        self._last_record = AgentRecord(meta=RecordMeta(title="Kitty worker"))

    async def start(self) -> None:
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                return
            if self._stopped:
                raise RuntimeError(f"worker has been stopped: {self.session_id}")
            self._task = asyncio.create_task(self._run(), name=f"kitty-worker:{self.session_id}")

    async def dispatch(
        self,
        message: str,
        *,
        record: AgentRecord | None = None,
        request_id: str | None = None,
    ) -> WorkerResult:
        if not message.strip():
            raise ValueError("message cannot be empty")
        await self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[WorkerResult] = loop.create_future()
        item = _WorkerRequest(
            request_id=request_id or uuid.uuid4().hex,
            message=message,
            record=record or AgentRecord(meta=RecordMeta(title="CLI")),
            future=future,
        )
        await self._queue.put(item)
        return await future

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._task is None:
            return
        await self._queue.put(_STOP)
        await self._task

    async def _run(self) -> None:
        await self._emit(
            SessionEvent.create(EventType.WORKER_STARTED, self.session_id),
            self._last_record,
        )
        self.logger.info("worker started")
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    break
                assert isinstance(item, _WorkerRequest)
                await self._handle(item)
            finally:
                self._queue.task_done()

        await self._emit(
            SessionEvent.create(EventType.WORKER_STOPPED, self.session_id),
            self._last_record,
        )
        self.logger.info("worker stopped")

    async def _handle(self, request: _WorkerRequest) -> None:
        self._last_record = request.record
        user_id = request.record.effective_user_id()
        try:
            state = await asyncio.to_thread(self.store.load, self.session_id)
            await self._emit(
                SessionEvent.wire(
                    self.session_id,
                    WireType.TURN_BEGIN,
                    user_input=request.message,
                    user_id=user_id,
                    request_id=request.request_id,
                ),
                request.record,
            )

            async def emit_wire(wire_type: WireType, payload: dict) -> None:
                await self._emit(
                    SessionEvent.wire(
                        self.session_id,
                        wire_type,
                        request_id=request.request_id,
                        **payload,
                    ),
                    request.record,
                )

            result = await self.agent.run(request.message, state.messages, emit_wire)
            state.messages.extend(result.messages)
            state.messages = state.messages[-self.max_history_messages :]
            state.metadata.update(
                {
                    "last_request_id": request.request_id,
                    "last_user_id": user_id,
                }
            )
            await asyncio.to_thread(self.store.save, state)

            await emit_wire(WireType.TURN_END, {"reply": result.reply, "steps": result.steps})
            await self._emit(
                SessionEvent.create(
                    EventType.CLI_TURN_DONE,
                    self.session_id,
                    {
                        "request_id": request.request_id,
                        "reply": result.reply,
                        "steps": result.steps,
                    },
                ),
                request.record,
            )
            if not request.future.done():
                request.future.set_result(
                    WorkerResult(
                        request_id=request.request_id,
                        session_id=self.session_id,
                        reply=result.reply,
                        steps=result.steps,
                    )
                )
        except Exception as exc:
            self.logger.exception("turn failed request_id=%s", request.request_id)
            await self._emit(
                SessionEvent.create(
                    EventType.WORKER_FAILED,
                    self.session_id,
                    {
                        "request_id": request.request_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                ),
                request.record,
            )
            if not request.future.done():
                request.future.set_exception(exc)

    async def _emit(self, event: SessionEvent, record: AgentRecord) -> None:
        context = HookContext(
            record=record,
            work_dir=self.work_dir,
            session_id=self.session_id,
            logger=self.logger,
        )
        await self.hooks.emit(event, context)
