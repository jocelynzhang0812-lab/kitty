from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from kitty.channels.base import ChannelMessage
from kitty.core.context import AgentRecord, RecordMeta


@dataclass(slots=True, frozen=True)
class FeishuChallenge:
    challenge: str


@dataclass(slots=True, frozen=True)
class UnsupportedFeishuMessage:
    session_id: str
    message_type: str
    request_id: str | None
    record: AgentRecord


class FeishuEventParser:
    """Parses the observable subset of Feishu event schema 2.0."""

    def __init__(
        self,
        *,
        require_mention: bool = True,
        dedupe_size: int = 2048,
        verification_token: str = "",
        encrypt_key: str = "",
        dedupe: Callable[[str], bool] | None = None,
        max_clock_skew_seconds: int = 300,
    ):
        self.require_mention = require_mention
        self.dedupe_size = dedupe_size
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.dedupe = dedupe
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()

    def parse_http(
        self,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> FeishuChallenge | ChannelMessage | UnsupportedFeishuMessage | None:
        """Verify the raw request, decrypt it when configured, then parse it."""

        normalized = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        if self.encrypt_key:
            self._verify_signature(body, normalized)
        try:
            outer = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Feishu body must be valid JSON") from exc
        if not isinstance(outer, dict):
            raise ValueError("Feishu body must be a JSON object")
        encrypted = outer.get("encrypt")
        if encrypted is not None:
            if not self.encrypt_key:
                raise ValueError("encrypted Feishu event received without FEISHU_ENCRYPT_KEY")
            payload = self._decrypt_event(str(encrypted))
        else:
            payload = outer
        return self.parse(payload)

    def parse(
        self, payload: dict[str, Any]
    ) -> FeishuChallenge | ChannelMessage | UnsupportedFeishuMessage | None:
        header = payload.get("header") or {}
        if self.verification_token:
            supplied = str(payload.get("token") or header.get("token") or "")
            if supplied != self.verification_token:
                raise ValueError("invalid Feishu verification token")
        challenge = payload.get("challenge")
        if isinstance(challenge, str):
            return FeishuChallenge(challenge)

        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        message = event.get("message") or {}
        message_id = str(message.get("message_id") or "")
        if message_id and not self._accept_once(message_id):
            return None

        mentions = message.get("mentions") or []
        chat_type = str(message.get("chat_type") or "")
        if self.require_mention and chat_type != "p2p" and not mentions:
            return None
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        user_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "")
        chat_id = str(message.get("chat_id") or "")
        session_id = chat_id or user_id or message_id
        meta = RecordMeta(
            title="飞书私聊" if chat_type == "p2p" else "飞书群聊",
            channel="feishu",
            extra={
                "message_id": message_id,
                "chat_type": chat_type,
                "mentioned": bool(mentions) or chat_type == "p2p",
            },
        )
        record = AgentRecord(
            user_id=user_id,
            from_user=user_id,
            sender=user_id,
            chat_id=chat_id,
            meta=meta,
        )
        message_type = str(message.get("message_type") or "")
        if message_type != "text":
            return UnsupportedFeishuMessage(
                session_id=session_id,
                message_type=message_type or "unknown",
                request_id=message_id or None,
                record=record,
            )
        try:
            content = json.loads(message.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        text = str(content.get("text") or "").strip()
        text = re.sub(r"<at\b[^>]*>.*?</at>", "", text, flags=re.IGNORECASE).strip()
        for mention in mentions:
            name = str((mention or {}).get("name") or "").strip()
            if name:
                text = re.sub(rf"^@{re.escape(name)}\s*", "", text).strip()
        text = re.sub(r"^@_user_\d+\s*", "", text).strip()
        text = re.sub(r"^@\S+\s*", "", text).strip()
        if not text:
            return None

        return ChannelMessage(
            session_id=session_id,
            content=text,
            request_id=message_id or None,
            record=record,
        )

    def _accept_once(self, message_id: str) -> bool:
        if self.dedupe is not None:
            return bool(self.dedupe(message_id))
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self.dedupe_size:
            self._seen.discard(self._seen_order.popleft())
        return True

    def _verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        timestamp = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        supplied = headers.get("x-lark-signature", "")
        if not timestamp or not nonce or not supplied:
            raise ValueError("missing Feishu signature headers")
        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise ValueError("invalid Feishu request timestamp") from exc
        if self.max_clock_skew_seconds > 0:
            if abs(time.time() - request_time) > self.max_clock_skew_seconds:
                raise ValueError("stale Feishu request timestamp")
        signature_input = (timestamp + nonce + self.encrypt_key).encode("utf-8") + body
        expected = hashlib.sha256(signature_input).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid Feishu request signature")

    def _decrypt_event(self, encrypted: str) -> dict[str, Any]:
        try:
            from Crypto.Cipher import AES
        except ImportError as exc:
            raise RuntimeError(
                "pycryptodome is required for encrypted Feishu events"
            ) from exc
        try:
            raw = base64.b64decode(encrypted, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid Feishu encrypted payload") from exc
        if len(raw) <= AES.block_size or len(raw) % AES.block_size != 0:
            raise ValueError("invalid Feishu encrypted payload length")
        key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
        iv, ciphertext = raw[: AES.block_size], raw[AES.block_size :]
        plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
        if not plaintext:
            raise ValueError("empty Feishu decrypted payload")
        padding = plaintext[-1]
        if padding < 1 or padding > AES.block_size:
            raise ValueError("invalid Feishu PKCS7 padding")
        if plaintext[-padding:] != bytes([padding]) * padding:
            raise ValueError("invalid Feishu PKCS7 padding")
        try:
            payload = json.loads(plaintext[:-padding].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("decrypted Feishu payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("decrypted Feishu payload must be an object")
        return payload


JsonTransport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


class FeishuSender:
    """Dependency-free Feishu text sender with tenant-token caching."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        transport: JsonTransport | None = None,
        timeout_seconds: float = 10.0,
        min_send_interval_seconds: float = 0.21,
    ):
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._post_json
        self.min_send_interval_seconds = max(0.0, min_send_interval_seconds)
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_send_by_chat: dict[str, float] = {}

    async def send_text(
        self,
        chat_id: str,
        text: str,
        request_uuid: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self._send_text_sync,
            chat_id,
            text,
            request_uuid,
        )

    def _send_text_sync(
        self,
        chat_id: str,
        text: str,
        request_uuid: str | None = None,
    ) -> dict[str, Any]:
        if not chat_id:
            raise ValueError("chat_id is required")
        token = self._tenant_token()
        self._wait_for_send_slot(chat_id)
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid[:50]
        result = self.transport(
            url,
            {"Authorization": f"Bearer {token}"},
            payload,
        )
        if result.get("code") != 0:
            # A stale/invalid token is one common source of API-level errors.
            # Clearing it is safe and lets the durable retry fetch a fresh one.
            with self._token_lock:
                self._token = ""
                self._token_expires_at = 0.0
            raise RuntimeError(f"Feishu send failed: code={result.get('code')} msg={result.get('msg')}")
        return result

    def _wait_for_send_slot(self, chat_id: str) -> None:
        if self.min_send_interval_seconds <= 0:
            return
        now = time.monotonic()
        with self._rate_lock:
            slot = max(now, self._next_send_by_chat.get(chat_id, now))
            self._next_send_by_chat[chat_id] = slot + self.min_send_interval_seconds
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def _tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 60:
                return self._token
            result = self.transport(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {},
                {"app_id": self.app_id, "app_secret": self.app_secret},
            )
            if result.get("code") != 0 or not result.get("tenant_access_token"):
                raise RuntimeError(
                    f"Feishu token failed: code={result.get('code')} msg={result.get('msg')}"
                )
            self._token = str(result["tenant_access_token"])
            self._token_expires_at = now + float(result.get("expire") or 7200)
            return self._token

    def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"Feishu HTTP {exc.code}: {detail}") from exc
