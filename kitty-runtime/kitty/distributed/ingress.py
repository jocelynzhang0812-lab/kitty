from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from kitty.channels.codec import serialize_channel_message
from kitty.channels.feishu import (
    FeishuChallenge,
    FeishuEventParser,
    UnsupportedFeishuMessage,
)
from kitty.deployment import DeploymentSettings
from kitty.distributed.config import DistributedSettings
from kitty.memory.postgres_store import PostgresStore


class DistributedIngressApp:
    """Stateless Feishu ingress: validate, persist, acknowledge."""

    def __init__(
        self,
        store: PostgresStore,
        parser: FeishuEventParser,
        *,
        max_body_bytes: int = 1_048_576,
    ):
        self.store = store
        self.parser = parser
        self.max_body_bytes = max_body_bytes
        self.logger = logging.getLogger("kitty.distributed.ingress")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        if method == "GET" and path == "/health":
            await _respond(send, 200, {"ok": True, "status": "alive", "role": "server"})
            return
        if method == "GET" and path == "/ready":
            try:
                ready = await asyncio.to_thread(self.store.ping)
                counts = await asyncio.to_thread(self.store.job_counts)
            except Exception:
                self.logger.exception("database readiness check failed")
                ready, counts = False, {}
            await _respond(
                send,
                200 if ready else 503,
                {"ok": ready, "status": "ready" if ready else "unavailable", "jobs": counts},
            )
            return
        if method != "POST" or path != "/feishu/events":
            await _respond(send, 404, {"ok": False, "error": "not found"})
            return
        try:
            body = await _read_body(receive, self.max_body_bytes)
            parsed = self.parser.parse_http(body, _headers(scope))
            result = await self._accept(parsed)
            await _respond(send, 200, result)
        except ValueError as exc:
            await _respond(send, 400, {"ok": False, "error": str(exc)})
        except Exception:
            self.logger.exception("distributed ingress failed")
            await _respond(send, 503, {"ok": False, "error": "ingress unavailable"})

    async def _accept(self, parsed) -> dict[str, Any]:
        if isinstance(parsed, FeishuChallenge):
            return {"challenge": parsed.challenge}
        if parsed is None:
            return {"ok": True, "skipped": True}
        if isinstance(parsed, UnsupportedFeishuMessage):
            payload = {
                "kind": "reply",
                "session_id": parsed.session_id,
                "request_id": parsed.request_id,
                "chat_id": parsed.record.chat_id,
                "reply": "目前暂时只能处理文字消息，请用文字描述您的问题，我会继续帮您。",
            }
        else:
            payload = serialize_channel_message(parsed)
        job_id = str(payload.get("request_id") or "")
        session_id = str(payload.get("session_id") or "")
        created = await asyncio.to_thread(
            self.store.enqueue_inbox,
            job_id,
            session_id,
            payload,
        )
        return {
            "ok": True,
            "accepted": created,
            "duplicate": not created,
            "session_id": session_id,
        }

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await asyncio.to_thread(self.store.ensure_schema)
                    await asyncio.to_thread(self.store.ping)
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    return
            elif message["type"] == "lifespan.shutdown":
                await asyncio.to_thread(self.store.close)
                await send({"type": "lifespan.shutdown.complete"})
                return


def create_ingress_app() -> DistributedIngressApp:
    deployment = DeploymentSettings.from_env()
    distributed = DistributedSettings.from_env("server")
    if deployment.environment == "production":
        missing = [
            name
            for name, value in {
                "FEISHU_VERIFICATION_TOKEN": deployment.feishu_verification_token,
                "FEISHU_ENCRYPT_KEY": deployment.feishu_encrypt_key,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError("missing distributed server settings: " + ", ".join(missing))
    store = PostgresStore(
        distributed.database_url,
        max_pool_size=max(4, distributed.concurrency * 2),
    )
    parser = FeishuEventParser(
        verification_token=deployment.feishu_verification_token,
        encrypt_key=deployment.feishu_encrypt_key,
        require_mention=deployment.feishu_require_mention,
        max_clock_skew_seconds=deployment.feishu_max_clock_skew_seconds,
        accept_images=deployment.feishu_accept_images,
        # PostgreSQL's unique inbox job_id is the distributed dedupe boundary.
        # Parsing must remain retryable if the database write itself fails.
        dedupe=lambda _event_id: True,
    )
    return DistributedIngressApp(store, parser)


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


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


async def _respond(send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
