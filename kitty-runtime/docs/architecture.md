# 推断出的 Kitty 架构

当前实现把 Kitty 作为宿主运行时，把 CS-bot 作为部署在其上的领域 Agent。

```text
Feishu HTTPS webhook
    -> signature / AES / token / dedupe
    -> KittyASGIApp
        -> WorkerManager
            -> SessionWorker (per-session serialized queue)
                -> CSBotTurnHandler
                    -> CSAgent
                        -> Model client
                        -> 16 business tools
                        -> local knowledge index
                -> HookBus
                -> SQLiteSessionStore
        -> FeishuSender
```

## 组件职责

- `KittyASGIApp`：存活/就绪探针、飞书回调、调试 API、后台任务和优雅关闭；
- `FeishuEventParser`：签名、时钟偏差、AES 解密、token、消息类型、@ 规则；
- `WorkerManager`：按会话创建 worker，不同会话并发；
- `SessionWorker`：同一会话严格串行，发布 turn 生命周期事件；
- `CSBotTurnHandler`：启动真实 CS-bot、检查知识库、恢复上下文并执行消息；
- `SQLiteSessionStore`：持久化消息和飞书 `message_id` 幂等记录；
- `HookBus`：隔离 hook 超时与异常；
- `FeishuSender`：获取 tenant access token 并代表机器人发消息。

## 部署约束

- 当前 SQLite 数据必须挂载持久卷；
- SQLite 幂等表只对单实例可靠，当前生产形态应保持一个副本；
- HTTPS/TLS 由网关或反向代理终止；
- 模型和飞书密钥由部署平台注入；
- `/health` 只说明进程存活，流量切换应以 `/ready` 为准；
- 任一生产必需配置或 CS-bot 知识库初始化失败时，进程启动失败。

## 兼容依据与未知项

仓库明确暴露了 worker 日志、Python hook、`cli.wire` / `cli.turn_done`、wire payload、hook context、skills 和文件型长期记忆等接口。

仓库无法证明原始 Kitty 是否使用远程队列、容器、特定模型网关或独立授权系统，因此本项目只声明对可观察接口和 CS-bot 实际运行路径的兼容，不声明内部实现一致。
