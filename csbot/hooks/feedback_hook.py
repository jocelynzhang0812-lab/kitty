"""Kitty Event Hook：自动将 CS Bot 对话写入飞书多维表格，并在需要人工时 @ 章璟菲。

挂载方式：在 worker 配置（或 session 的 event_hooks）中添加：
    "/root/cs-bot/csbot/hooks/feedback_hook.py"

监听事件：
- cli.wire    ：捕获用户输入和 Bot 回复
- cli.turn_done：一轮对话结束，触发表格写入 + 转人工通知
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

# 确保 csbot 包可被导入（worker 进程的 PYTHONPATH 可能不包含 /root/cs-bot）
CSBOT_ROOT = "/root/cs-bot"
if CSBOT_ROOT not in sys.path:
    sys.path.insert(0, CSBOT_ROOT)

# 尝试加载 .env（如果 worker 没有预先加载）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from csbot.storage.bitable import BitableClient
from csbot.integrations.feishu import FeishuIntegration

# 模块级状态：按 session_id 缓存本轮对话内容
_session_buffers: dict[str, dict[str, Any]] = {}

# 去重集合：已经通知过人工的 session，避免重复 @
_human_notified_sessions: set[str] = set()

# 初始化 Bitable 客户端（延迟初始化，避免模块导入时失败）
_bitable: BitableClient | None = None

# 初始化 Feishu 客户端（延迟初始化）
_feishu: FeishuIntegration | None = None


def _get_bitable() -> BitableClient:
    global _bitable
    if _bitable is None:
        _bitable = BitableClient()
    return _bitable


def _get_feishu() -> FeishuIntegration:
    global _feishu
    if _feishu is None:
        chat_id_map = {}
        user_id_map = {}
        if os.getenv("INTERNAL_DEBUG_CHAT_ID"):
            chat_id_map["internal_debug_group"] = os.getenv("INTERNAL_DEBUG_CHAT_ID")
        if os.getenv("FEEDBACK_BOT_USER_ID"):
            user_id_map["feedback_bot"] = os.getenv("FEEDBACK_BOT_USER_ID")
        _feishu = FeishuIntegration(
            app_id=os.getenv("FEISHU_APP_ID", ""),
            app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            chat_id_map=chat_id_map,
            user_id_map=user_id_map,
        )
    return _feishu


def _extract_text(content: str | list) -> str:
    """从 user_input 或 ContentPart 列表中提取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                pt = part.get("type", "")
                if pt == "text":
                    parts.append(part.get("text", ""))
                elif pt == "image_url":
                    parts.append("[图片]")
                elif pt == "audio_url":
                    parts.append("[音频]")
                elif pt == "video_url":
                    parts.append("[视频]")
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return str(content)


def _clean_meta_prefix(text: str) -> str:
    """去掉 Kitty worker 添加的元数据前缀 [user_id: ...] [time: ...] [群聊: ...]，只保留用户原话。"""
    # 去掉 [user_id: xxx] [time: xxx] [群聊: xxx] 等标签
    text = re.sub(r'\[user_id:\s*[^\]]+\]\s*', '', text)
    text = re.sub(r'\[time:\s*[^\]]+\]\s*', '', text)
    text = re.sub(r'\[群聊:\s*[^\]]+\]\s*', '', text)
    return text.strip()


# Hook 声明：告诉 Kitty 我们关心哪些事件
listened_events = ["cli.wire", "cli.turn_done"]


async def hook(event, ctx):
    """Event hook 主入口。

    Args:
        event: SessionEvent（包含 event_type, session_id, data, timestamp）
        ctx:   HookContext（包含 record, work_dir, session_id 等）
    """
    event_type = event.event_type
    session_id = event.session_id
    data = event.data

    if event_type == "cli.wire":
        await _handle_wire(session_id, data)
    elif event_type == "cli.turn_done":
        await _handle_turn_done(session_id, ctx)


async def _handle_wire(session_id: str, data: dict) -> None:
    """处理 cli.wire 事件，累积用户输入和 Bot 回复。"""
    wire = data.get("wire", {})
    wire_type = wire.get("wire_type", "")

    # 初始化会话缓冲区
    buf = _session_buffers.setdefault(session_id, {
        "user_message": "",
        "bot_reply_parts": [],
        "user_id": "",
    })

    if wire_type == "TurnBegin":
        user_input = wire.get("user_input", "")
        raw_text = _extract_text(user_input)
        buf["user_message"] = _clean_meta_prefix(raw_text)
        buf["bot_reply_parts"] = []
        # 尝试从 wire 获取用户 ID
        buf["user_id"] = wire.get("user_id") or wire.get("from_user") or ""

    elif wire_type == "TextPart":
        text = wire.get("text", "")
        if text:
            buf["bot_reply_parts"].append(text)

    elif wire_type == "ContentPart":
        # ContentPart 可能是文本、图片等
        part_type = wire.get("type", "")
        if part_type == "text":
            text = wire.get("text", "")
            if text:
                buf["bot_reply_parts"].append(text)


async def _handle_turn_done(session_id: str, ctx) -> None:
    """处理 cli.turn_done 事件，将累积的对话内容写入多维表格，并检测转人工。"""
    buf = _session_buffers.pop(session_id, None)
    if not buf:
        return

    user_message = buf.get("user_message", "")
    bot_reply = " ".join(buf.get("bot_reply_parts", []))

    if not user_message and not bot_reply:
        return

    # ── 1. 转人工检测：用户消息或 Bot 回复包含转人工关键词时，@ 章璟菲 ──
    human_keywords = {"转人工", "找人工", "要人工", "人工客服", "找真人", "找客服", "接人工", "换人"}
    is_human_request = any(kw in user_message for kw in human_keywords)
    is_human_reply = any(kw in bot_reply for kw in human_keywords)

    if (is_human_request or is_human_reply) and session_id not in _human_notified_sessions:
        # 组装用户信息：优先从 ctx 获取，其次从 wire 缓存获取
        user_name = "未知用户"
        try:
            # Kitty ctx.record 可能包含 user_id / sender / from_user 等信息
            if hasattr(ctx, "record") and ctx.record:
                record = ctx.record
                if hasattr(record, "user_id") and record.user_id:
                    user_name = str(record.user_id)
                elif hasattr(record, "from_user") and record.from_user:
                    user_name = str(record.from_user)
                elif hasattr(record, "sender") and record.sender:
                    user_name = str(record.sender)
        except Exception:
            pass

        # 如果 ctx 没拿到，尝试从 wire 缓存拿
        if user_name == "未知用户":
            wire_user_id = buf.get("user_id", "")
            if wire_user_id:
                user_name = wire_user_id

        # 截断用户消息，避免通知过长
        user_msg_short = user_message[:120] + "..." if len(user_message) > 120 else user_message

        try:
            feishu = _get_feishu()
            notice_text = (
                f"有用户需要人工客服协助，请尽快处理。\n"
                f"用户: {user_name}\n"
                f"反馈: {user_msg_short}\n"
                f"会话ID: {session_id}"
            )
            result = await feishu.send_text(
                "internal_debug_group",
                notice_text,
                at_users=["feedback_bot"],
            )
            if result.get("code") == 0:
                _human_notified_sessions.add(session_id)
                ctx.logger.info(
                    f"[FeedbackHook] 已通知内部群 @章璟菲 | session={session_id} | "
                    f"msg_id={result.get('data', {}).get('message_id', '')}"
                )
            else:
                ctx.logger.warning(
                    f"[FeedbackHook] 通知内部群失败 | session={session_id} | "
                    f"code={result.get('code')} | msg={result.get('msg')}"
                )
        except Exception as e:
            ctx.logger.warning(f"[FeedbackHook] 通知内部群异常: {e}")

    # ── 2. 写入多维表格 ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 从 wire 缓存或 ctx 获取 user_id
    user_id = buf.get("user_id", "")
    if not user_id and hasattr(ctx, "record") and ctx.record:
        try:
            record_obj = ctx.record
            user_id = (
                getattr(record_obj, "user_id", "")
                or getattr(record_obj, "from_user", "")
                or getattr(record_obj, "sender", "")
            )
        except Exception:
            pass

    record = {
        "反馈时间": now_str,
        "反馈来源": ctx.record.meta.title if ctx.record.meta else "飞书群聊",
        "用户ID": user_id,
        "反馈内容": user_message,
        "问题类型": "其他",
        "处理状态": "待处理",
    }

    try:
        bitable = _get_bitable()
        result = await bitable.create_raw(fields=record)
        if result.get("code") == 0:
            ctx.logger.info(
                f"[FeedbackHook] 写入成功 | session={session_id} | "
                f"record_id={result.get('data', {}).get('record', {}).get('record_id', '')}"
            )
        else:
            ctx.logger.warning(
                f"[FeedbackHook] 写入失败 | code={result.get('code')} | msg={result.get('msg')}"
            )
    except Exception as e:
        ctx.logger.warning(f"[FeedbackHook] 写入异常: {e}")


hook.listened_events = listened_events
