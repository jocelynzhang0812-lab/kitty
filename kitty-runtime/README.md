# Kitty Feishu Runtime

Kitty Runtime 提供安全 Webhook、模型工具循环、会话 worker、持久化投递和通用扩展接口。

## 运行模式

- 开发环境：没有 `LLM_API_KEY` 时自动使用 mock provider；
- 生产环境：强制要求真实模型、飞书凭据、Verification Token 和 Encrypt Key；
- 单聊直接响应；群聊默认仅响应 @ 机器人；
- 非文字消息返回文字引导。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m kitty --once "hello"
```

## 生产启动

```bash
cp .env.production.example ../.env.production
set -a
source ../.env.production
set +a
.venv/bin/uvicorn kitty.server:create_app --factory --host 0.0.0.0 --port 8000
```

接口：

- `GET /health`：进程存活；
- `GET /ready`：运行时和持久化投递状态；
- `POST /feishu/events`：飞书事件入口；
- `POST /v1/messages`：可选调试入口，生产默认关闭。

## 扩展点

- `KITTY_SYSTEM_PROMPT`：机器人角色与回答规则；
- `KITTY_TOOL_MODULES`：加载实现 `register_tools(registry)` 的 Python 模块；
- `KITTY_HOOK_PATHS`：加载监听生命周期事件的 Python Hook；
- `.agents/*/skills/*/SKILL.md`：按用户消息选择并注入 Skills；
- `AGENTS.md` / `MEMORY.md`：从 `KITTY_PROJECT_ROOT` 只读加载项目上下文。

中性示例位于 `examples/tools.py` 和 `examples/echo_hook.py`。

## 持久化投递

飞书消息在 HTTP 确认前写入 SQLite。模型回复也会在发送前保存；发送失败只重试飞书调用，不重复运行模型和工具。超过最大尝试次数的任务进入 `dead`：

```bash
kitty --state-dir /data/kitty --delivery-status
kitty --state-dir /data/kitty --retry-delivery om_xxx
```

重放后重启服务，启动恢复器会继续处理任务。

## 文档

- [系统架构](docs/architecture.md)
- [事件协议](docs/event-protocol.md)
- [飞书生产部署](docs/production-deployment.md)
