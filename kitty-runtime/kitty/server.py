from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

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
            await _respond(
                send,
                status,
                {
                    "ok": self.runtime.started,
                    "status": "ready" if self.runtime.started else "starting",
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
                task = asyncio.create_task(self.reply_sender(parsed.record.chat_id, reply))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_done)
                return {"ok": True, "accepted": True, "session_id": parsed.session_id}
            return {"ok": True, "reply": reply, "session_id": parsed.session_id}
        if self.reply_sender is not None:
            task = asyncio.create_task(self._process_feishu_message(parsed))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_done)
            return {"ok": True, "accepted": True, "session_id": parsed.session_id}
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

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
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
        dedupe=runtime.store.accept_event,
        max_clock_skew_seconds=settings.feishu_max_clock_skew_seconds,
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
        health_details=settings.public_summary(),
        debug_api_enabled=settings.environment != "production"
        or bool(settings.debug_api_token),
        debug_api_token=settings.debug_api_token,
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
