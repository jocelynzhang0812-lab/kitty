#!/usr/bin/env python3
"""Kimi Claw CS Bot —— LLM Agent 架构入口"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()

from csbot.agent.llm import LLMClient, CSAgent
from csbot.agent.session import SessionStore

from csbot.nlp.intake import CSIntakeSkill
from csbot.nlp.emotion import CSEmotionSkill
from csbot.nlp.clarify import CSClarifySkill

from csbot.sops.guardrails import CSGuardrailsSkill
from csbot.sops.output_reviewer import CSOutputReviewerSkill
from csbot.sops.self_check import CSSelfCheckSkill
from csbot.sops.responses import CSResponseTemplatesSkill
from csbot.sops.router import CSSOPRouterSkill
from csbot.sops.self_diagnosis import CSSelfDiagnosisSkill
from csbot.sops.follow_up import CSFollowUpSOP
from csbot.sops.human_handoff import CSHumanHandoffSkill

from csbot.feedback.report import CSBugReportSkill
from csbot.feedback.product_feedback import CSProductFeedbackSkill
from csbot.feedback.tracker import CSTicketTrackerSkill
from csbot.feedback.collector import CSFeedbackCollectorSkill

from csbot.storage.daily import CSDailyReportSkill
from csbot.storage.bitable import BitableClient

from csbot.integrations.feishu import FeishuIntegration

from csbot.knowledge.loader import KnowledgeLoader
from csbot.knowledge.index import KnowledgeIndex
from csbot.knowledge.kb_skill import KBSearchSkill
from csbot.knowledge.embeddings import OpenAIEmbeddingProvider


async def bootstrap(project_root: str | Path | None = None) -> CSAgent:
    """初始化所有组件，返回 Agent"""

    root = Path(project_root or Path(__file__).resolve().parent).expanduser().resolve()

    # 0. 向量 Embedding Provider（可选）
    # 支持环境变量：EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL
    # 无配置时自动回退到纯关键词检索
    provider = None
    if os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY"):
        try:
            provider = OpenAIEmbeddingProvider(
                model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            )
            print("[bootstrap] EmbeddingProvider 已启用（OpenAI 兼容 API）")
        except Exception as e:
            print(f"[bootstrap] EmbeddingProvider 初始化失败，回退到关键词检索: {e}")

    # 1. 知识库（优先加载本地知识库，禁止无意义联网搜索）
    loader = KnowledgeLoader()
    index = KnowledgeIndex()
    # 始终使用绝对路径，避免服务进程的工作目录影响知识库加载。
    knowledge_dirs = [root / "csbot" / "knowledge" / "data"]
    legacy_knowledge = root / "知识库0501"
    if legacy_knowledge.is_dir():
        knowledge_dirs.append(legacy_knowledge)
    docs = loader.load_all(",".join(str(path) for path in knowledge_dirs))
    if not docs:
        raise RuntimeError(f"知识库为空，无法启动 CS Bot: {knowledge_dirs[0]}")
    index.add_batch(docs)

    # 若启用了向量检索，批量编码所有文档（启动时执行一次）
    if provider is not None:
        await index.build_embeddings(provider)
        print(f"[bootstrap] 知识库向量索引构建完成: {len(docs)} 条文档")

    # 2. 注册所有 Skills（自动注册到 ToolRegistry）
    KBSearchSkill(index, provider=provider)
    CSIntakeSkill()
    CSEmotionSkill()
    CSClarifySkill()
    CSGuardrailsSkill()
    CSOutputReviewerSkill()
    CSSelfCheckSkill()
    CSResponseTemplatesSkill()
    CSSOPRouterSkill()
    CSSelfDiagnosisSkill()
    CSFollowUpSOP()
    CSHumanHandoffSkill()
    CSBugReportSkill()
    # CSTicketTrackerSkill 在 bitable 初始化后再注册
    CSDailyReportSkill()

    # 3. 基础设施（强制从环境变量读取，无 fallback）
    sessions = SessionStore()
    # 内部通知目标必须由部署环境显式提供，避免把测试环境 ID 带入生产。
    chat_id_map = {}
    user_id_map = {}
    if os.getenv("INTERNAL_DEBUG_CHAT_ID"):
        chat_id_map["internal_debug_group"] = os.getenv("INTERNAL_DEBUG_CHAT_ID")
    if os.getenv("FEEDBACK_BOT_USER_ID"):
        user_id_map["feedback_bot"] = os.getenv("FEEDBACK_BOT_USER_ID")
    if os.getenv("FEISHU_CHAT_ID_MAP"):
        try:
            chat_id_map.update(json.loads(os.getenv("FEISHU_CHAT_ID_MAP")))
        except json.JSONDecodeError:
            print("[bootstrap] FEISHU_CHAT_ID_MAP 格式错误，应为 JSON 字符串")
    if os.getenv("FEISHU_USER_ID_MAP"):
        try:
            user_id_map.update(json.loads(os.getenv("FEISHU_USER_ID_MAP")))
        except json.JSONDecodeError:
            print("[bootstrap] FEISHU_USER_ID_MAP 格式错误，应为 JSON 字符串")
    feishu = FeishuIntegration(
        app_id=os.getenv("FEISHU_APP_ID"),
        app_secret=os.getenv("FEISHU_APP_SECRET"),
        chat_id_map=chat_id_map,
        user_id_map=user_id_map,
    )
    bitable = BitableClient(
        app_token=os.getenv("BITABLE_APP_TOKEN"),
        table_id=os.getenv("BITABLE_TABLE_ID"),
        feishu_app_id=os.getenv("FEISHU_APP_ID"),
        feishu_secret=os.getenv("FEISHU_APP_SECRET"),
    )

    # 产品建议表格（独立 bitable）
    pf_app_token = os.getenv("PRODUCT_FEEDBACK_APP_TOKEN")
    pf_table_id = os.getenv("PRODUCT_FEEDBACK_TABLE_ID")
    if pf_app_token and pf_table_id:
        CSProductFeedbackSkill(
            bitable=bitable,
            app_token=pf_app_token,
            table_id=pf_table_id,
        )
        print("[bootstrap] 产品建议表格已启用")
    else:
        print("[bootstrap] 产品建议表格未配置，跳过")

    # 2.5 注册工单 Tracker（接入 Bitable，支持独立工单表）
    CSTicketTrackerSkill(
        bitable=bitable,
        app_token=os.getenv("TICKET_APP_TOKEN") or os.getenv("BITABLE_APP_TOKEN"),
        table_id=os.getenv("TICKET_TABLE_ID") or os.getenv("BITABLE_TABLE_ID"),
    )

    # 2.6 注册通用反馈收集器（每条消息自动入表）
    CSFeedbackCollectorSkill(bitable=bitable)
    print("[bootstrap] 通用反馈收集器已启用")

    # 4. LLM Client
    llm = LLMClient(
        api_key=os.getenv("LLM_API_KEY") or os.getenv("KIMI_API_KEY"),
        model=os.getenv("LLM_MODEL") or os.getenv("KIMI_MODEL", "kimi-latest"),
        base_url=os.getenv("LLM_BASE_URL")
        or os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
    )

    # 5. Agent
    agent = CSAgent(llm=llm, sessions=sessions, feishu=feishu, bitable=bitable)

    # 6. 将产品建议 bitable 配置注入 agent 上下文（供 _run_loop 中的工具使用）
    agent._product_feedback_config = {
        "app_token": os.getenv("PRODUCT_FEEDBACK_APP_TOKEN"),
        "table_id": os.getenv("PRODUCT_FEEDBACK_TABLE_ID"),
    }
    return agent


async def demo():
    agent = await bootstrap()

    user_id = "ou_xxx"
    session_id = "group_user_001"

    test_cases = [
        "Kimi Claw 是什么？",
        "我的Claw在飞书群里@没反应，Bot ID 123456789",
        "太差了！我要退款！！",
        "为什么我的Claw失忆了，昨天聊的都不记得",
        "重启了还是不行，没用",
    ]

    for msg in test_cases:
        print(f"\n[User] {msg}")
        reply = await agent.handle_message(user_id, session_id, msg)
        print(f"[Bot] {reply}")


if __name__ == "__main__":
    asyncio.run(demo())
