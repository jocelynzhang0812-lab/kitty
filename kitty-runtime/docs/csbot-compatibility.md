# CS-bot 接入说明

Kitty 负责飞书通道、会话 worker、持久化、生命周期事件和部署；CS-bot 负责客服提示词、知识检索、业务工具和飞书业务集成。

生产模式使用 `KITTY_AGENT_MODE=csbot`。服务启动时会自动：

1. 从 `KITTY_PROJECT_ROOT` 加载仓库根目录的 `main.py`；
2. 调用 `bootstrap(project_root=...)` 初始化 CS-bot；
3. 校验 `search_knowledge_base` 工具存在；
4. 校验知识库已经索引且文档数大于零；
5. 将 Kitty 的持久化历史恢复到 CS-bot 会话；
6. 把 CS-bot 回复转换为 Kitty 的 `TextPart` 和 turn 生命周期事件。

任何关键初始化失败都会让 `/ready` 保持不可用，并使服务启动失败，不会静默降级成 mock bot。

## 反馈 hook

如需启用 `csbot/hooks/feedback_hook.py`，设置：

```text
KITTY_CSBOT_FEEDBACK_HOOK=1
```

hook 接收当前 CS-bot 所依赖的字段：

- `event.event_type`、`event.session_id`、`event.data`；
- `ctx.record` 及其中的用户、会话和通道元数据；
- `ctx.work_dir`、`ctx.session_id`、`ctx.logger`；
- `TurnBegin`、`TextPart` 和 `cli.turn_done` 生命周期事件。

hook 会产生真实的飞书群通知或多维表写入。开发和测试环境默认不启用；生产启用前应先用测试应用和测试表验收字段、权限与收件人。

## 模型配置

CS-bot 和 Kitty 统一读取下面的配置，`LLM_*` 优先，旧的 `KIMI_*` 名称继续兼容：

```text
LLM_API_KEY=...
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_MODEL=kimi-latest
```

密钥只能通过运行环境注入，不应写入仓库、镜像或日志。
