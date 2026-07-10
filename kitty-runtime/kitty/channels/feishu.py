from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from kitty.channels.base import ChannelMessage
from kitty.core.context import AgentRecord, RecordMeta


@dataclass(slots=True, frozen=True)
class FeishuChallenge:
    challenge: str


class FeishuEventParser:
    """Parses the observable subset of Feishu event schema 2.0."""

    def __init__(
        self,
        *,
        require_mention: bool = True,
        dedupe_size: int = 2048,
        verification_token: str = "",
    ):
        self.require_mention = require_mention
        self.dedupe_size = dedupe_size
        self.verification_token = verification_token
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()

    def parse(self, payload: dict[str, Any]) -> FeishuChallenge | ChannelMessage | None:
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
        if message.get("message_type") != "text":
            return None

        mentions = message.get("mentions") or []
        if self.require_mention and not mentions:
            return None
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

        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        user_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "")
        chat_id = str(message.get("chat_id") or "")
        session_id = chat_id or user_id or message_id
        return ChannelMessage(
            session_id=session_id,
            content=text,
            request_id=message_id or None,
            record=AgentRecord(
                user_id=user_id,
                from_user=user_id,
                sender=user_id,
                chat_id=chat_id,
                meta=RecordMeta(title="飞书群聊", channel="feishu", extra={"message_id": message_id}),
            ),
        )

    def _accept_once(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self.dedupe_size:
            self._seen.discard(self._seen_order.popleft())
        return True


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
    ):
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._post_json
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    async def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(self._send_text_sync, chat_id, text)

    def _send_text_sync(self, chat_id: str, text: str) -> dict[str, Any]:
        if not chat_id:
            raise ValueError("chat_id is required")
        token = self._tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        result = self.transport(
            url,
            {"Authorization": f"Bearer {token}"},
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu send failed: code={result.get('code')} msg={result.get('msg')}")
        return result

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
