# Kitty

Kitty 是一个面向飞书的生产级 Agent，采用Server和Worker分离的架构，目标是稳定接收事件、调用工具、回复飞书，并且在生产环境里能扩容、恢复、审计和隔离风险。

## 架构总览

Kitty 有两种运行模式：

一是单进程模式，适合本地联调、低流量机器人和 10 分钟上线：

```mermaid
flowchart TD
    Feishu["飞书 Webhook"] --> Parser["FeishuEventParser<br/>验签 · 解密 · Token · 去重"]
    Parser --> Queue["SQLite Durable Queue"]
    Queue --> Runtime["KittyRuntime"]
    Runtime --> WorkerManager["WorkerManager"]
    WorkerManager --> SessionWorker["SessionWorker<br/>同会话串行 · 不同会话并发"]
    SessionWorker --> AgentLoop["AgentLoop"]
    AgentLoop --> Model["OpenAI-compatible Model"]
    AgentLoop --> Tools["Tools<br/>in-process / subprocess / container"]
    AgentLoop --> Skills["SKILL.md"]
    SessionWorker --> Hooks["HookBus"]
    SessionWorker --> Store["SQLite Session Store"]
    Runtime --> Sender["FeishuSender<br/>tenant token · uuid 幂等 · 限速"]
```

二是分布式模式，适合真实生产和独立扩容：

```mermaid
flowchart LR
    Feishu["飞书 Webhook"] --> Server["Kitty Server × N<br/>只验签、解密、落 Inbox、ACK"]
    Server --> DB["PostgreSQL<br/>Inbox · Session Lease · Sessions · Outbox"]
    DB --> Worker["Agent Worker × N<br/>模型 · 工具 · Skills · Hooks"]
    Worker --> DB
    DB --> Sender["Sender × N<br/>只发送飞书消息"]
    Sender --> Feishu
```

分布式模式里的职责边界很明确：

- `kitty server`：只处理飞书 HTTP 回调，完成安全校验后写入 PostgreSQL Inbox，然后立即 ACK；
- `kitty worker`：领取 Inbox Job，持有 Session Lease，运行模型和工具，保存会话，写入 Outbox；
- `kitty sender`：领取 Outbox Job，使用稳定 UUID 幂等发送飞书消息，失败只重试发送，不重新运行 Agent。

PostgreSQL 是生产协调层，负责 Inbox、Outbox、事件去重、会话历史、Session Lease 和 fencing token。`FOR UPDATE SKIP LOCKED` 用来让多个 Worker/Sender 安全并发领取任务。

## 关键模块

```text
kitty-runtime/
├── kitty/
│   ├── channels/          # 飞书事件解析、消息发送、卡片、图片
│   ├── agent/             # provider-neutral AgentLoop 和模型协议
│   ├── tools/             # ToolRegistry、执行器、子进程/容器隔离
│   ├── workers/           # 会话级 WorkerManager 和 SessionWorker
│   ├── memory/            # SQLite / PostgreSQL session 和队列存储
│   ├── distributed/       # server / worker / sender 三进程实现
│   ├── hooks/             # 生命周期事件 HookBus
│   ├── skills/            # SKILL.md 加载和选择
│   ├── server.py          # 单进程 ASGI 飞书服务
│   ├── runtime.py         # 运行时装配入口
│   └── cli.py             # setup / doctor / serve / server / worker / sender
├── docs/                  # 上线、生产部署、分布式部署和事件协议
├── examples/              # 工具和 Hook 示例
├── tests/                 # 单元测试和 PostgreSQL 集成测试
├── Dockerfile
└── pyproject.toml
```

## 飞书接入能力

Kitty 内置飞书生产接入需要的基础能力：

- Event v2.0 回调解析；
- Verification Token 校验；
- 飞书请求签名和时间戳校验；
- Encrypt Key AES-256-CBC 解密；
- URL challenge 响应；
- 单聊和群聊消息；
- 群聊按 `@` 触发；
- 文本消息、图片消息、卡片动作；
- 飞书消息卡片构建、更新、表情回应；
- tenant access token 缓存；
- 发送端稳定 `uuid`，避免重试导致重复消息；
- 失败退避重试和 dead letter；
- `/health` 和 `/ready` 探针。

## 工具和沙箱

工具通过 Python 模块注册：

```python
from kitty.tools.registry import ToolRegistry


def add(a: float, b: float) -> float:
    return a + b


def register_tools(registry: ToolRegistry) -> None:
    registry.add(
        "add",
        add,
        description="Add two numbers.",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    )
```

生产环境可以选择三种执行边界：

```text
KITTY_TOOL_EXECUTOR=in_process   # 默认，本地调试最简单
KITTY_TOOL_EXECUTOR=subprocess   # 每次工具调用进入独立 Python 子进程
KITTY_TOOL_EXECUTOR=container    # 每次工具调用进入短生命周期 Docker 容器
```

`subprocess` 模式能在工具超时后直接杀掉子进程。`container` 模式默认使用：

- `--network none`
- `--read-only`
- `--cap-drop ALL`
- `no-new-privileges`
- CPU / 内存 / pids 限制
- tmpfs `/tmp`
- 只读 workspace mount

容器模式示例：

```text
KITTY_TOOL_EXECUTOR=container
KITTY_TOOL_CONTAINER_IMAGE=kitty-runtime:latest
KITTY_TOOL_CONTAINER_WORKSPACE=/app/kitty-runtime
KITTY_TOOL_CONTAINER_NETWORK=none
KITTY_TOOL_CONTAINER_MEMORY=256m
KITTY_TOOL_CONTAINER_CPUS=1
KITTY_TOOL_CONTAINER_PIDS_LIMIT=128
KITTY_TOOL_CONTAINER_TMPFS_SIZE=64m
```

使用 `subprocess` 或 `container` 时，工具 handler 必须是可导入函数。lambda 或闭包需要显式提供 `handler_ref="module:function"`。

## 和 OpenClaw 相比

| 维度 | Kitty | OpenClaw |
| --- | --- | --- |
| 产品定位 | 飞书优先的生产级 Agent Runtime | 多渠道个人 AI assistant  |
| 接入面 | 专注飞书 Webhook、卡片、图片、发送幂等 | 覆盖大量聊天渠道，Feishu 是其中之一 |
| 上线复杂度 | 少量 Python 配置；`setup` / `doctor` / `serve` 面向飞书联调 | 功能更全，但 Gateway、channel、skills、daemon 配置面更大 |
| 生产队列 | 内置 SQLite durable queue；分布式模式用 PostgreSQL Inbox / Outbox | 更偏个人 Gateway 和多渠道事件分发 |
| 横向扩容 | Server / Worker / Sender 三角色可独立扩容 | 重点是本地/自托管 personal assistant，扩容不是 Feishu 接入的主路径 |
| 会话一致性 | PostgreSQL Session Lease + fencing token | 依赖 OpenClaw 自身 session / sandbox / agent routing 体系 |
| 密钥边界 | Server、Worker、Sender 三角色 env 分离 | 多渠道统一 Gateway 配置，能力更广但边界更复杂 |
| 工具安全 | in-process / subprocess / Docker container 三档 | OpenClaw 有 sandbox 体系，生态更大，默认/策略需按 channel 和 agent 配置 |
| 适合场景 | 企业自建飞书 Agent、客服/运营/内部助手、需要稳定回调和队列恢复 | 个人全能助理、多渠道自动化、桌面/移动 companion 体验 |

Kitty 的主要优势：

1. **飞书路径更短。** Kitty 的主链路就是 `Feishu -> Server -> Worker -> Sender -> Feishu`，不需要先理解一个多渠道 Gateway 生态。
2. **生产失败模型更清晰。** 入站事件先落 Inbox，模型运行后写 Outbox，发送失败只重试 Sender，不会重新跑 Agent。
3. **Worker 可以真正独立扩容。** 多个 Worker 通过 PostgreSQL 领取任务，同一会话由 Session Lease 串行保护，不同会话可以并发。
4. **密钥暴露面更小。** Server 不需要模型密钥，Worker 不需要飞书 App Secret，Sender 不需要模型密钥。
5. **飞书联调更直接。** `setup` 生成回调地址，`doctor --live` 同时检测模型和飞书凭据，`/ready` 可用于部署探针。
6. **工具隔离是运行时一等能力。** 生产 Worker 可以把工具放到子进程或 Docker 容器中，便于限制超时、输出和系统权限。

Kitty 的劣势也很明确：

- 不提供 OpenClaw 那样的多渠道生态；
- 没有 companion app、voice、Canvas、ClawHub 这类成熟体验；
- 内置工具数量少，需要你按业务自己写 Python tools；
- container sandbox 是工具级隔离，不是完整多租户安全平台；
- 如果你要做个人全能助理，OpenClaw 的现成能力更丰富。

简单判断：

- **只做飞书企业自建 Agent：优先 Kitty。**
- **要一个跨很多聊天软件的个人助理：优先 OpenClaw。**
- **要把 OpenClaw 的 Agent 能力包装进飞书生产服务：可以用 Kitty 做飞书入口和生产队列，用工具调用去桥接外部 Agent。**

## 10 分钟联调

本地准备：

```bash
git clone https://github.com/jocelynzhang0812-lab/kitty.git
cd kitty/kitty-runtime
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
```

启动配置向导：

```bash
.venv/bin/kitty setup
```

向导会让你填写：

- 机器人名称和系统提示词；
- OpenAI-compatible 模型地址、模型名和 API Key；
- 飞书 App ID / App Secret；
- Verification Token / Encrypt Key；
- 公网 HTTPS base URL；
- 可选的 Tools 和 Hooks。

检查配置：

```bash
.venv/bin/kitty doctor --env-file .env --live
```

启动单进程服务：

```bash
.venv/bin/kitty serve --env-file .env --host 0.0.0.0 --port 8000
```

飞书事件订阅地址：

```text
https://你的域名/feishu/events
```

联调时建议先测：

1. 飞书保存事件订阅 URL，确认 challenge 通过；
2. 单聊发送一条文本，确认机器人回复；
3. 群聊不 `@` 不回复；
4. 群聊 `@` 后回复；
5. 重启进程后再发消息，确认会话历史仍在；
6. 如果开启图片，发送图片消息确认 `image_key` 能进入工具/Hook；
7. 如果开启卡片，点击按钮确认 `card.action.trigger` 能走同一条链路。

完整流程见：

- [10 分钟上线指南](kitty-runtime/docs/ten-minute-launch.md)
- [飞书生产部署指南](kitty-runtime/docs/production-deployment.md)
- [分布式部署指南](kitty-runtime/docs/distributed-deployment.md)

## 分布式部署

复制三份角色配置：

```bash
cp kitty-runtime/.env.server.example kitty-runtime/.env.server
cp kitty-runtime/.env.worker.example kitty-runtime/.env.worker
cp kitty-runtime/.env.sender.example kitty-runtime/.env.sender
```

启动：

```bash
docker compose -f docker-compose.distributed.yml up --build -d
```

扩容：

```bash
docker compose -f docker-compose.distributed.yml up -d \
  --scale worker=4 \
  --scale sender=2
```

角色密钥边界：

```text
.env.server  # Verification Token / Encrypt Key
.env.worker  # Model API Key / Tools / Hooks / Workspace
.env.sender  # Feishu App ID / App Secret
```

运维命令：

```bash
KITTY_DATABASE_URL=postgresql://... kitty jobs
KITTY_DATABASE_URL=postgresql://... kitty retry-job inbox JOB_ID
KITTY_DATABASE_URL=postgresql://... kitty retry-job outbox JOB_ID
```

## 常用环境变量

```text
KITTY_ENV=production
KITTY_BOT_NAME=Team Assistant
KITTY_SYSTEM_PROMPT=You are a helpful Feishu assistant.

LLM_API_KEY=...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=...
LLM_TIMEOUT_SECONDS=120

FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
FEISHU_VERIFICATION_TOKEN=...
FEISHU_ENCRYPT_KEY=...
FEISHU_REQUIRE_MENTION=1
FEISHU_ACCEPT_IMAGES=0

KITTY_TOOL_MODULES=examples.tools
KITTY_HOOK_PATHS=
KITTY_TOOL_EXECUTOR=subprocess
KITTY_TOOL_DENYLIST=
KITTY_TOOL_MAX_OUTPUT_BYTES=65536
```

## 测试

```bash
cd kitty-runtime
.venv/bin/python -m unittest discover -s tests -v
```

如果要跑 PostgreSQL 分布式测试：

```bash
KITTY_TEST_POSTGRES_URL=postgresql://kitty:kitty@127.0.0.1:55432/kitty_test \
  .venv/bin/python -m unittest discover -s tests -v
```
