from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kitty.channels.base import ChannelMessage
from kitty.channels.feishu import (
    FeishuChallenge,
    FeishuEventParser,
    FeishuSender,
    UnsupportedFeishuMessage,
)
from kitty.core.context import AgentRecord, RecordMeta
from kitty.deployment import DeploymentSettings, build_runtime
from kitty.runtime import KittyRuntime


ReplySender = Callable[[str, str], Awaitable[Any]]
IdempotentReplySender = Callable[[str, str, str], Awaitable[Any]]


class KittyASGIApp:
    """Dependency-free ASGI API for health, debug chat, and Feishu events."""

    def __init__(
        self,
        runtime: KittyRuntime,
        *,
        feishu_parser: FeishuEventParser | None = None,
        reply_sender: ReplySender | None = None,
        max_body_bytes: int = 1_048_576,
        health_details: dict[str, Any] | None = None,
        debug_api_enabled: bool = True,
        debug_api_token: str = "",
        idempotent_reply_sender: IdempotentReplySender | None = None,
        max_delivery_attempts: int = 5,
        delivery_retry_base_seconds: float = 1.0,
    ):
        self.runtime = runtime
        self.feishu_parser = feishu_parser or FeishuEventParser()
        self.reply_sender = reply_sender
        self.max_body_bytes = max_body_bytes
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._logger = logging.getLogger("kitty.server")
        self.health_details = dict(health_details or {})
        self.debug_api_enabled = debug_api_enabled
        self.debug_api_token = debug_api_token
        self.idempotent_reply_sender = idempotent_reply_sender
        self.max_delivery_attempts = max(1, max_delivery_attempts)
        self.delivery_retry_base_seconds = max(0.01, delivery_retry_base_seconds)
        self._delivery_tasks: dict[str, asyncio.Task[Any]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        if method == "GET" and path == "/health":
            await _respond(send, 200, {"ok": True, "status": "alive"})
            return
        if method == "GET" and path == "/ready":
            status = 200 if self.runtime.started else 503
            delivery = await asyncio.to_thread(self.runtime.store.feishu_job_counts)
            await _respond(
                send,
                status,
                {
                    "ok": self.runtime.started,
                    "status": "ready" if self.runtime.started else "starting",
                    "delivery": delivery,
                    **self.health_details,
                },
            )
            return
        if method == "POST" and path in {"/v1/messages", "/feishu/events"}:
            headers = _headers(scope)
            if path == "/v1/messages":
                if not self.debug_api_enabled:
                    await _respond(send, 404, {"ok": False, "error": "not found"})
                    return
                if self.debug_api_token and not _valid_debug_token(
                    headers, self.debug_api_token
                ):
                    await _respond(send, 401, {"ok": False, "error": "unauthorized"})
                    return
            try:
                body = await _read_body(receive, self.max_body_bytes)
                if path == "/v1/messages":
                    result = await self._message(_decode_json(body))
                else:
                    result = await self._feishu_parsed(
                        self.feishu_parser.parse_http(body, headers)
                    )
                await _respond(send, 200, result)
            except ValueError as exc:
                await _respond(send, 400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._logger.exception("request failed path=%s", path)
                await _respond(send, 500, {"ok": False, "error": "internal server error"})
            return
        await _respond(send, 404, {"ok": False, "error": "not found"})

    async def _message(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        message = str(payload.get("message") or payload.get("text") or "")
        if not session_id or not message.strip():
            raise ValueError("session_id and message are required")
        user_id = str(payload.get("user_id") or "http-user")
        result = await self.runtime.dispatch(
            session_id,
            message,
            request_id=str(payload.get("request_id") or "") or None,
            record=AgentRecord(
                user_id=user_id,
                from_user=user_id,
                sender=user_id,
                chat_id=str(payload.get("chat_id") or session_id),
                meta=RecordMeta(title="HTTP", channel="http"),
            ),
        )
        return {"ok": True, "session_id": session_id, "reply": result.reply, "steps": result.steps}

    async def _feishu_parsed(self, parsed) -> dict[str, Any]:
        if isinstance(parsed, FeishuChallenge):
            return {"challenge": parsed.challenge}
        if parsed is None:
            return {"ok": True, "skipped": True}
        if isinstance(parsed, UnsupportedFeishuMessage):
            reply = "目前暂时只能处理文字消息，请用文字描述您的问题，我会继续帮您。"
            if self.reply_sender is not None and parsed.record.chat_id:
                return await self._enqueue_feishu_delivery(
                    job_id=parsed.request_id,
                    payload={
                        "kind": "reply",
                        "session_id": parsed.session_id,
                        "request_id": parsed.request_id,
                        "chat_id": parsed.record.chat_id,
                        "reply": reply,
                    },
                )
            return {"ok": True, "reply": reply, "session_id": parsed.session_id}
        if self.reply_sender is not None:
            return await self._enqueue_feishu_delivery(
                job_id=parsed.request_id,
                payload=_serialize_channel_message(parsed),
            )
        result = await self._process_feishu_message(parsed)
        return {"ok": True, "reply": result.reply, "session_id": parsed.session_id}

    async def _process_feishu_message(self, parsed):
        result = await self.runtime.dispatch(
            parsed.session_id,
            parsed.content,
            request_id=parsed.request_id,
            record=parsed.record,
        )
        if self.reply_sender is not None and parsed.record.chat_id:
            await self.reply_sender(parsed.record.chat_id, result.reply)
        return result

    async def _enqueue_feishu_delivery(
        self,
        *,
        job_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        durable_id = job_id or f"generated-{uuid.uuid4().hex}"
        created = await asyncio.to_thread(
            self.runtime.store.enqueue_feishu_job,
            durable_id,
            payload,
        )
        if created:
            self._schedule_feishu_delivery(durable_id)
        return {
            "ok": True,
            "accepted": created,
            "duplicate": not created,
            "session_id": str(payload.get("session_id") or ""),
        }

    def _schedule_feishu_delivery(self, job_id: str) -> None:
        existing = self._delivery_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_feishu_delivery(job_id),
            name=f"kitty-feishu-delivery:{job_id}",
        )
        self._delivery_tasks[job_id] = task
        self._background_tasks.add(task)
        task.add_done_callback(
            lambda done, delivery_id=job_id: self._background_done(
                done, delivery_id
            )
        )

    async def _run_feishu_delivery(self, job_id: str) -> None:
        while True:
            current = await asyncio.to_thread(self.runtime.store.load_feishu_job, job_id)
            if current is None or current.status in {"completed", "dead"}:
                return
            if current.status == "processing":
                return
            wait_seconds = max(0.0, current.available_at - time.time())
            if wait_seconds:
                await asyncio.sleep(wait_seconds)
            job = await asyncio.to_thread(self.runtime.store.claim_feishu_job, job_id)
            if job is None:
                await asyncio.sleep(0)
                continue

            try:
                reply = job.reply_text
                if not job.reply_ready:
                    if job.payload.get("kind") == "reply":
                        reply = str(job.payload.get("reply") or "")
                    else:
                        message = _deserialize_channel_message(job.payload)
                        result = await self.runtime.dispatch(
                            message.session_id,
                            message.content,
                            request_id=message.request_id,
                            record=message.record,
                        )
                        reply = result.reply
                    await asyncio.to_thread(
                        self.runtime.store.save_feishu_reply,
                        job_id,
                        reply,
                    )

                chat_id = str(job.payload.get("chat_id") or "")
                if not chat_id:
                    raise ValueError("Feishu delivery has no chat_id")
                request_uuid = hashlib.sha256(
                    f"kitty-feishu:{job_id}".encode("utf-8")
                ).hexdigest()[:50]
                if self.idempotent_reply_sender is not None:
                    await self.idempotent_reply_sender(chat_id, reply, request_uuid)
                elif self.reply_sender is not None:
                    await self.reply_sender(chat_id, reply)
                else:
                    raise RuntimeError("Feishu reply sender is not configured")
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    self.runtime.store.retry_feishu_job,
                    job_id,
                    "delivery interrupted during shutdown",
                    0,
                )
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if job.attempts >= self.max_delivery_attempts:
                    await asyncio.to_thread(
                        self.runtime.store.fail_feishu_job,
                        job_id,
                        error,
                    )
                    self._logger.error(
                        "Feishu delivery exhausted retries job_id=%s error=%s",
                        job_id,
                        error,
                    )
                    return
                delay = min(
                    self.delivery_retry_base_seconds * (2 ** (job.attempts - 1)),
                    300.0,
                )
                await asyncio.to_thread(
                    self.runtime.store.retry_feishu_job,
                    job_id,
                    error,
                    delay,
                )
                self._logger.warning(
                    "Feishu delivery retry job_id=%s attempt=%s delay=%.2fs error=%s",
                    job_id,
                    job.attempts,
                    delay,
                    error,
                )
            else:
                await asyncio.to_thread(self.runtime.store.complete_feishu_job, job_id)
                return

    def _background_done(
        self,
        task: asyncio.Task[Any],
        delivery_id: str | None = None,
    ) -> None:
        self._background_tasks.discard(task)
        if delivery_id is not None:
            self._delivery_tasks.pop(delivery_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error("Feishu background turn failed: %s", error)

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.runtime.start()
                    pending = await asyncio.to_thread(
                        self.runtime.store.recover_feishu_jobs
                    )
                    if self.reply_sender is not None:
                        for job_id in pending:
                            self._schedule_feishu_delivery(job_id)
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    self._logger.exception("runtime startup failed")
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    return
            elif message["type"] == "lifespan.shutdown":
                if self._background_tasks:
                    done, pending = await asyncio.wait(self._background_tasks, timeout=10)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                await self.runtime.close()
                await send({"type": "lifespan.shutdown.complete"})
                return


def create_app() -> KittyASGIApp:
    """Uvicorn factory: ``uvicorn kitty.server:create_app --factory``."""

    settings = DeploymentSettings.from_env()
    settings.validate()
    runtime = build_runtime(settings)
    parser = FeishuEventParser(
        verification_token=settings.feishu_verification_token,
        encrypt_key=settings.feishu_encrypt_key,
        require_mention=settings.feishu_require_mention,
        max_clock_skew_seconds=settings.feishu_max_clock_skew_seconds,
        accept_images=settings.feishu_accept_images,
    )
    sender = (
        FeishuSender(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
        )
        if settings.feishu_app_id and settings.feishu_app_secret
        else None
    )
    return KittyASGIApp(
        runtime,
        feishu_parser=parser,
        reply_sender=sender.send_text if sender else None,
        idempotent_reply_sender=sender.send_text if sender else None,
        health_details=settings.public_summary(),
        debug_api_enabled=settings.environment != "production"
        or bool(settings.debug_api_token),
        debug_api_token=settings.debug_api_token,
        max_delivery_attempts=settings.delivery_max_attempts,
        delivery_retry_base_seconds=settings.delivery_retry_base_seconds,
    )


async def _read_body(receive, max_body_bytes: int) -> bytes:
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > max_body_bytes:
            raise ValueError("request body is too large")
        more = bool(message.get("more_body", False))
    return bytes(body or b"{}")


def _decode_json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _valid_debug_token(headers: dict[str, str], expected: str) -> bool:
    supplied = headers.get("x-kitty-api-token", "")
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _serialize_channel_message(message: ChannelMessage) -> dict[str, Any]:
    meta = message.record.meta
    return {
        "kind": "message",
        "session_id": message.session_id,
        "content": message.content,
        "request_id": message.request_id,
        "chat_id": message.record.chat_id,
        "record": {
            "user_id": message.record.user_id,
            "from_user": message.record.from_user,
            "sender": message.record.sender,
            "chat_id": message.record.chat_id,
            "meta": {
                "title": meta.title if meta else "",
                "channel": meta.channel if meta else "",
                "extra": meta.extra if meta else {},
            },
        },
    }


def _deserialize_channel_message(payload: dict[str, Any]) -> ChannelMessage:
    record_data = payload.get("record") or {}
    meta_data = record_data.get("meta") or {}
    return ChannelMessage(
        session_id=str(payload.get("session_id") or ""),
        content=str(payload.get("content") or ""),
        request_id=str(payload.get("request_id") or "") or None,
        record=AgentRecord(
            user_id=str(record_data.get("user_id") or ""),
            from_user=str(record_data.get("from_user") or ""),
            sender=str(record_data.get("sender") or ""),
            chat_id=str(record_data.get("chat_id") or ""),
            meta=RecordMeta(
                title=str(meta_data.get("title") or ""),
                channel=str(meta_data.get("channel") or ""),
                extra=dict(meta_data.get("extra") or {}),
            ),
        ),
    )


async def _respond(send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})
