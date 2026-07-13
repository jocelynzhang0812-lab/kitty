# Kitty Runtime

Kitty Runtime 提供安全 Webhook、模型工具循环、会话 worker、持久化投递和通用扩展接口。

## 运行模式

- 生产环境：强制要求真实模型、飞书凭据、Verification Token 和 Encrypt Key；
- 单聊直接响应；群聊默认仅响应 @ 机器人；
- 非文字消息返回文字引导。

## 本地运行

```bash
# Kitty requires Python 3.11+.
# On macOS, install it first if `python3 --version` is still 3.9:
# brew install python@3.12
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install .
.venv/bin/python -m kitty --once "hello"
```

## 10 分钟上线

```bash
.venv/bin/kitty setup
```

该命令只监听 `127.0.0.1`，打开一个带随机访问令牌的本地向导。它会：

- 引导填写机器人职责、模型和飞书应用信息；
- 真实检测模型 API、飞书凭据、持久化目录、Tools 和 Hooks；
- 仅在全部检查通过后保存 `.env`，文件权限为 `0600`；
- 给出可直接复制到飞书的事件回调地址。

保存后运行：

```bash
.venv/bin/kitty doctor --env-file .env --live
.venv/bin/kitty serve --env-file .env
```

`doctor --live` 会向模型发送一个极小请求，并向飞书换取 tenant token。完整步骤见[10 分钟上线指南](docs/ten-minute-launch.md)。

## 手动生产启动

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

## 分布式生产启动

分布式模式需要 PostgreSQL，并将运行职责拆成三个命令：

```bash
kitty server --env-file .env.server
kitty worker --env-file .env.worker
kitty sender --env-file .env.sender
```

- `server`：只处理飞书 HTTP、验签、Inbox 落盘和快速确认；
- `worker`：领取 Inbox、持有 Session Lease、运行 Agent、写入 Outbox；
- `sender`：领取 Outbox、幂等发送飞书、独立重试和死信。

查看和重放分布式任务：

```bash
KITTY_DATABASE_URL=postgresql://... kitty jobs
KITTY_DATABASE_URL=postgresql://... kitty retry-job inbox JOB_ID
KITTY_DATABASE_URL=postgresql://... kitty retry-job outbox JOB_ID
```

参见[分布式部署指南](docs/distributed-deployment.md)。

## 扩展点

- `KITTY_SYSTEM_PROMPT`：机器人角色与回答规则；
- `KITTY_TOOL_MODULES`：加载实现 `register_tools(registry)` 的 Python 模块；
- `KITTY_TOOL_EXECUTOR`：`in_process` 或 `subprocess`，生产 Worker 推荐 `subprocess`；
- `KITTY_TOOL_DENYLIST`：逗号分隔的运行时禁用工具名；
- `KITTY_TOOL_MAX_OUTPUT_BYTES`：单次工具结果写回模型前的最大 JSON 字节数；
- `KITTY_TOOL_CONTAINER_IMAGE`：`KITTY_TOOL_EXECUTOR=container` 时使用的工具沙箱镜像；
- `KITTY_HOOK_PATHS`：加载监听生命周期事件的 Python Hook；
- `.agents/*/skills/*/SKILL.md`：按用户消息选择并注入 Skills；
- `AGENTS.md` / `MEMORY.md`：从 `KITTY_PROJECT_ROOT` 只读加载项目上下文。

`subprocess` 模式会为每次工具调用启动独立 Python 子进程，并在超时后直接终止该子进程。该模式要求工具 handler 是可导入函数；lambda 或闭包需要在注册时提供 `handler_ref="module:function"`。

`container` 模式会通过短生命周期 Docker 容器运行同一个工具协议，默认无网络、只读根文件系统、无新增权限，并设置 CPU、内存、进程数和 tmpfs 限制。它需要配置 `KITTY_TOOL_CONTAINER_IMAGE`，且运行 Worker 的环境必须能启动 Docker 容器。

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
- [10 分钟上线](docs/ten-minute-launch.md)
- [飞书生产部署](docs/production-deployment.md)
- [分布式部署](docs/distributed-deployment.md)
