# Kitty

Kitty 是一个面向飞书的生产级 Agent，采用 Server 和 Worker 分离的架构，目标是稳定接收事件、调用工具、回复飞书，并且在生产环境里能扩容、恢复、审计和隔离风险。

> 想最快跑通？直接跳到 [快速联调（不加密）](#快速联调不加密最小配置)。
> 遇到报错？直接跳到 [故障排查](#故障排查)，里面覆盖了 400、doctor 报错、"暂时无法和机器人发消息"、模型 401 等常见坑。

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

## 消息处理链路（联调时对着排查）

一条飞书消息从进入到回复，会经过下面这些环节。**排障时先定位卡在哪一环**：

```text
飞书客户端发消息
   │  ← 卡这里：飞书报"暂时无法和机器人发消息"（应用没发布 / 隧道断了 / 回调地址不通）
   ▼
公网隧道 (cloudflared 等)
   │  ← 卡这里：隧道失效，URL 变了或进程挂了
   ▼
POST /feishu/events  (server.py)
   │  ← 卡这里：返回 400（验签 / 解密 / token / 环境配置问题）
   ▼
FeishuEventParser 解析  (channels/feishu.py)
   │
   ▼
SQLite Durable Queue → SessionWorker → AgentLoop
   │  ← 卡这里：模型 API 报 401 / 模型名不存在，重试耗尽进 dead letter
   ▼
FeishuSender 回复飞书
```

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
│   ├── onboarding.py      # setup / doctor 的配置校验与连通性检查
│   ├── deployment.py      # 环境变量装配与部署级校验
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
- Verification Token 校验（可选，配了才校验）；
- 飞书请求签名和时间戳校验（配了 Encrypt Key 才校验）；
- Encrypt Key AES-256-CBC 解密（可选）；
- URL challenge 响应；
- 单聊和群聊消息；
- 群聊按 `@` 触发；
- 文本消息、图片消息、卡片动作；
- 飞书消息卡片构建、更新、表情回应；
- tenant access token 缓存；
- 发送端稳定 `uuid`，避免重试导致重复消息；
- 失败退避重试和 dead letter；
- `/health` 和 `/ready` 探针。

## 快速联调（不加密最小配置）

这是最快跑通的路径：**只需要模型 API + 飞书 App ID/Secret**，不配 Verification Token / Encrypt Key，用 `development` 环境跳过一切签名与 token 校验。适合本地联调，**不适合正式上线**（见 [走向生产](#走向生产)）。

### 1. 拉代码 & 建虚拟环境

```bash
git clone https://github.com/jocelynzhang0812-lab/kitty.git
cd kitty/kitty-runtime
sh scripts/bootstrap-local.sh
```

如果提示本机没有 Python 3.11+，macOS 先装 Python 3.12：

```bash
brew install python@3.12
KITTY_PYTHON="$(brew --prefix python@3.12)/bin/python3.12" sh scripts/bootstrap-local.sh
```

> **⚠️ 强烈建议用 editable 方式安装**，否则你改了源码不会生效（`.venv` 里跑的是 site-packages 的旧副本）：
>
> ```bash
> .venv/bin/pip install -e .
> ```
>
> 判断是否 editable：`.venv/lib/python*/site-packages/` 下如果有 `kitty/` 实体目录而不是 `__editable__*.pth`，说明是非 editable 安装，改源码不生效。

### 2. 写最小 `.env`

在 **`kitty-runtime/` 目录下**新建一个 `.env`（下面的 `serve --env-file .env` 用的是相对路径，务必在这个目录里执行；不确定时先 `pwd` 确认）：

```bash
# 关键：用 development，避免 production 强制要求加密字段
KITTY_ENV=development
KITTY_BOT_NAME=团队助手
KITTY_SYSTEM_PROMPT=You are a helpful internal Feishu assistant.
KITTY_MODEL_PROVIDER=openai_compatible
KITTY_PUBLIC_BASE_URL=https://你的隧道域名        # 见第 4 步

# 模型（以 DeepSeek 为例；模型名要用官方存在的，如 deepseek-chat）
LLM_API_KEY=sk-你的有效key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# 飞书（只需要这两个）
FEISHU_APP_ID=cli_你的app_id
FEISHU_APP_SECRET=你的app_secret
FEISHU_REQUIRE_MENTION=1        # 群聊必须 @ 才响应；单聊不受影响

# 不加密联调：以下两项留空/不写即可（服务端会跳过签名与 token 校验）
# FEISHU_VERIFICATION_TOKEN=
# FEISHU_ENCRYPT_KEY=
```

> 也可以用交互式向导 `.venv/bin/kitty setup` 生成 `.env`，但注意**网页版向导默认按「生产配置」校验**，会要求你填公网地址、Encrypt Key、Verification Token。想走不加密最小路径，直接手写上面的 `.env` 更省事。

### 3. 检查配置

```bash
.venv/bin/kitty doctor --env-file .env --live
```

`doctor` 会依次检查：配置完整性、持久化目录、扩展模块、**模型连接（真打一次模型接口）**、**飞书连接（用 app id/secret 换 token）**。全绿显示 `READY` 再继续。

### 4. 起服务 + 公网隧道

飞书回调必须是公网 HTTPS。本地联调可以用 cloudflared 临时隧道：

```bash
# 终端 A：起服务
.venv/bin/kitty serve --env-file .env --host 0.0.0.0 --port 8000

# 终端 B：起隧道，会打印一个 https://xxxx.trycloudflare.com
cloudflared tunnel --url http://localhost:8000
```

把隧道打印出的 HTTPS 地址填回 `.env` 的 `KITTY_PUBLIC_BASE_URL`（改完 **重启 serve** 才生效）。

> **隧道是临时的**：`trycloudflare` 每次重启 URL 都会变，进程一关就失效。联调期间别关终端 B。URL 变了要同步更新两处：`.env` 的 `KITTY_PUBLIC_BASE_URL` + 飞书后台回调地址。

### 5. 配置飞书开发者后台

在 [飞书开放平台](https://open.feishu.cn/) 你的企业自建应用里：

1. **应用能力**：添加「机器人」能力；
2. **事件订阅**：
   - 方式选「**将事件发送至开发者服务器**」（不是长连接）；
   - 回调地址填 `https://你的隧道域名/feishu/events`，保存时确认 **challenge 校验通过**；
   - **加密策略（Encrypt Key）保持清空**（对应上面不加密的配置）；
   - 订阅 `im.message.receive_v1`（接收消息）事件；
3. **版本管理与发布**：创建版本并**发布上线**，否则机器人不可用（会报"暂时无法和机器人发消息"）。

### 6. 逐项验证

1. 飞书后台保存事件订阅 URL，challenge 通过；
2. 单聊发一条文本，机器人回复；
3. 群聊不 `@` 不回复；
4. 群聊 `@` 后回复；
5. 重启进程后再发消息，确认会话历史仍在；
6. 如开启图片，发送图片确认 `image_key` 进入工具/Hook；
7. 如开启卡片，点击按钮确认 `card.action.trigger` 走同一条链路。

完整流程另见：

- [10 分钟上线指南](kitty-runtime/docs/ten-minute-launch.md)
- [飞书生产部署指南](kitty-runtime/docs/production-deployment.md)
- [分布式部署指南](kitty-runtime/docs/distributed-deployment.md)

## FAQ

### `/feishu/events` 返回 400 Bad Request

400 **只**在事件的「验签 / 解密 / token / JSON 解析」阶段失败时返回，跟你的机器人业务逻辑无关。**真正的原因写在 400 响应的 body 里**（`{"ok": false, "error": "..."}`），但 uvicorn 的 access log 只打状态码，看不到。先拿到 error 文案：

```bash
# 到飞书后台"事件订阅"页看响应内容，或本地自测：
curl -i -X POST http://127.0.0.1:8000/feishu/events \
  -H "Content-Type: application/json" -d '{"hello":"world"}'
```

对照下表定位：

| error 文案 | 原因 | 修复 |
| --- | --- | --- |
| `encrypted Feishu event received without FEISHU_ENCRYPT_KEY` | 飞书后台**开了加密**，但服务端没配 `FEISHU_ENCRYPT_KEY` | 飞书后台清空 Encrypt Key（不加密），或服务端补上 key |
| `invalid Feishu request signature` | 配了 `FEISHU_ENCRYPT_KEY` 但与飞书后台不一致 | 两边 Encrypt Key 完全一致，或都不用 |
| `invalid Feishu verification token` | `FEISHU_VERIFICATION_TOKEN` 与飞书后台不一致 | 复制飞书后台的 token 覆盖，或不配 |
| `missing Feishu signature headers` | 配了 Encrypt Key，但请求没带 `x-lark-*` 头（常见于代理丢头） | 反向代理透传 `x-lark-signature/timestamp/nonce` |
| `stale Feishu request timestamp` | 服务器时钟与飞书偏差超过 300s | NTP 同步时间 |
| `Feishu body must be valid JSON` | 网关篡改了 body，或不是飞书发来的请求 | 检查中间层 |

**最常见**：飞书后台开了加密而服务端没配（或两边 key 不一致）。不加密联调时，**飞书后台 Encrypt Key 必须清空**。

### doctor 报「缺少 xxx」但 serve 却能起来

这是设计上的**两套校验**：

- `serve` 用 [`deployment.py`](kitty-runtime/kitty/deployment.py) 校验，**只有 `KITTY_ENV=production` 才强制要求** Encrypt Key / Verification Token / 公网地址；`development` 下这些都是可选。
- `doctor` / 网页 `setup` 用 [`onboarding.py`](kitty-runtime/kitty/onboarding.py) 校验；网页版「保存生产配置」固定按 production 走，所以会要求加密字段和公网 HTTPS 地址。

**结论**：不加密联调请用 `KITTY_ENV=development` + 命令行 `doctor`/`serve`，不要走网页版「保存生产配置」。

### 改了源码不生效

`.venv` 若是用 `pip install .`（非 editable）装的，运行时加载的是 site-packages 里的副本，改源码目录无效。改成 editable：

```bash
cd kitty-runtime && .venv/bin/pip install -e .
```

### 飞书报「暂时无法和机器人发消息」

这是**飞书客户端侧**提示，通常请求还没走到你的服务。**分水岭：看 `serve` 终端发消息时有没有新日志**：

- **有** `POST /feishu/events` → 链路通了，去查后面的处理/回复环节；
- **完全没有** → 问题在飞书后台或隧道：
  1. 机器人能力没加 / **应用没发布**；
  2. 事件订阅没选「发送至开发者服务器」或没订阅 `im.message.receive_v1`；
  3. 隧道断了 / URL 变了（先 `curl https://隧道域名/health` 确认，正常返回 `{"ok": true, "status": "alive"}`）；
  4. 回调地址保存时 challenge 没通过。

### 模型报 401 / 模型不回复

`serve` 日志出现：

```text
Feishu delivery retry ... error=RuntimeError: model API returned HTTP 401:
  {"error":{"message":"Authentication Fails, Your api key: ****xxxx is invalid", ...}}
Feishu delivery exhausted retries ...
```

说明**飞书链路完全正常**，只是模型认证失败：

- **401 invalid api key**：`LLM_API_KEY` 无效/过期/复制截断 → 换有效 key；
- **model not found / 模型名报错**：`LLM_MODEL` 填了不存在的名字。DeepSeek 正确名是 `deepseek-chat`（V3）或 `deepseek-reasoner`（R1），没有 `deepseek-v4-flash` 这类；
- 改完 `.env` 后 **重启 serve**。注意：之前重试耗尽的消息已进 dead letter 不会自动重发，**发一条新消息**测试即可。

### 其它提醒

- **`.env` 改动必须重启 `serve` 才生效**；隧道进程与服务进程是两个，别混。
- `--env-file .env` 是相对路径，要在 `.env` 所在目录执行，或改成绝对路径。

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

## 环境变量

### 最小联调配置（不加密）

```text
KITTY_ENV=development
KITTY_BOT_NAME=Team Assistant
KITTY_SYSTEM_PROMPT=You are a helpful Feishu assistant.
KITTY_PUBLIC_BASE_URL=https://你的隧道域名

LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
FEISHU_REQUIRE_MENTION=1
```

### 生产完整配置

```text
KITTY_ENV=production
KITTY_BOT_NAME=Team Assistant
KITTY_SYSTEM_PROMPT=You are a helpful Feishu assistant.
KITTY_PUBLIC_BASE_URL=https://稳定域名

LLM_API_KEY=...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=...
LLM_TIMEOUT_SECONDS=120

FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
FEISHU_VERIFICATION_TOKEN=...   # production 必填
FEISHU_ENCRYPT_KEY=...          # production 必填，与飞书后台一致
FEISHU_REQUIRE_MENTION=1
FEISHU_ACCEPT_IMAGES=0

KITTY_TOOL_MODULES=examples.tools
KITTY_HOOK_PATHS=
KITTY_TOOL_EXECUTOR=subprocess
KITTY_TOOL_DENYLIST=
KITTY_TOOL_MAX_OUTPUT_BYTES=65536
```

## 走向生产

联调用的不加密 + 临时隧道**只适合本地**：不加密 + 不校验 token 意味着任何知道回调 URL 的人都能伪造飞书事件。上线时建议：

1. **开启加密与校验**：飞书后台配置 Encrypt Key + Verification Token，`.env` 两边配齐，`KITTY_ENV=production`；
2. **换固定公网域名**：用稳定 HTTPS 域名替代 `trycloudflare` 临时隧道；
3. **轮换联调期暴露过的密钥**：`LLM_API_KEY`、`FEISHU_APP_SECRET` 等；
4. **按需上分布式**：流量大时用 `server` / `worker` / `sender` 三角色独立扩容；
5. **工具隔离**：`KITTY_TOOL_EXECUTOR=subprocess` 或 `container`。

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
