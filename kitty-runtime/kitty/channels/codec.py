from __future__ import annotations

from typing import Any

from kitty.channels.base import ChannelMessage
from kitty.core.context import AgentRecord, RecordMeta


def serialize_channel_message(message: ChannelMessage) -> dict[str, Any]:
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


def deserialize_channel_message(payload: dict[str, Any]) -> ChannelMessage:
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
