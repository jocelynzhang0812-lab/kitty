# Kitty 架构

Kitty 将飞书通道、Agent 执行和业务扩展分离。

```text
Feishu HTTPS webhook
    -> FeishuEventParser
        -> signature / clock skew / AES / token / mention policy
    -> kitty_feishu_jobs (SQLite durable inbox/outbox)
    -> WorkerManager
        -> SessionWorker (per-session serialized queue)
            -> AgentLoop
                -> ModelProvider
                -> ToolRegistry
                -> SkillCatalog
            -> HookBus
            -> SQLiteSessionStore
    -> FeishuSender (rate pacing + idempotent uuid)
```

## 组件职责

- `KittyASGIApp`：HTTP、生命周期、快速确认、后台投递和优雅关闭；
- `FeishuEventParser`：验证所有入站事件并转换为通道消息；
- `WorkerManager`：按会话创建 worker，不同会话并发；
- `SessionWorker`：同一会话严格串行，持久化历史并发布事件；
- `AgentLoop`：调用模型、执行工具并限制最大步骤；
- `ToolRegistry`：工具 schema、allowlist、参数和超时边界；
- `SkillCatalog`：发现并按消息选择 `SKILL.md`；
- `HookBus`：隔离 Hook 的异常和超时；
- `SQLiteSessionStore`：会话、事件和投递任务；
- `FeishuSender`：tenant token 缓存、会话级节流和幂等发送。

## 扩展边界

框架不包含具体业务 Agent。机器人能力通过环境变量和项目文件注入：

```text
KITTY_SYSTEM_PROMPT
KITTY_TOOL_MODULES
KITTY_HOOK_PATHS
.agents/*/skills/*/SKILL.md
AGENTS.md
MEMORY.md
```

## 部署约束

- SQLite 文件必须挂载持久卷；
- SQLite 模式按单实例设计；
- HTTPS 由网关或反向代理终止；
- 流量探针使用 `/ready`，进程探针使用 `/health`；
- 密钥只从运行环境注入。

## 分布式模式

```text
Feishu -> Server -> PostgreSQL Inbox
                       -> Agent Worker -> PostgreSQL Session + Outbox
                                                -> Sender -> Feishu
```

- Server、Agent Worker 和 Sender 是独立进程，可分别扩容；
- `FOR UPDATE SKIP LOCKED` 保证任务只被一个实例领取；
- Session Lease 保证同一会话串行，fencing token 阻止失去租约的 Worker 回写；
- 任务租约过期后由其他实例自动恢复；
- Outbox 使用稳定 UUID 重试，发送失败不会重新运行模型；
- Worker 工作目录需要共享持久卷，或工具必须保持无本地状态。
