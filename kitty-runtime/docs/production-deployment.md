# 飞书生产部署指南

## 1. 准备飞书自建应用

1. 在飞书开放平台创建企业自建应用并启用机器人能力；
2. 为应用申请并发布以下消息权限：
   - 单聊收消息：`im:message.p2p_msg` 或对应只读权限；
   - 群聊 @ 消息：`im:message.group_at_msg` 或对应只读权限；
   - 机器人发消息：`im:message:send_as_bot`；
3. 添加事件 `im.message.receive_v1`；
4. 选择“将事件发送至开发者服务器”，地址填写 `https://你的域名/feishu/events`；
5. 配置 Verification Token 和 Encrypt Key，并把相同值安全注入服务环境；
6. 发布应用，并把机器人加入需要服务的群聊。

群聊默认只响应 @ 机器人；单聊直接响应。飞书可能重复投递同一消息，服务按 `message_id` 持久化去重。

发送端会按会话节流，并为每条回复附带稳定 `uuid`。飞书官方说明相同 `uuid` 在一小时内最多成功发送一次，因此网络错误重试不会重复刷屏。

官方参考：

- [订阅方式与开发者服务器](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server?lang=zh-CN)
- [Encrypt Key 加密和签名](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case)
- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)
- [获取 tenant access token](https://open.feishu.cn/document/server-docs/api-call-guide/calling-process/get-access-token)

## 2. 准备多维表格

把多维表授权给自建应用，并准备 `BITABLE_APP_TOKEN` 和 `BITABLE_TABLE_ID`。

当前核心反馈工具需要以下列名，列名必须完全一致：

| 列名 | 建议类型 |
| --- | --- |
| 反馈类型 | 单选或文本 |
| 用户描述 | 多行文本 |
| 影响功能 | 文本 |
| 处理状态 | 单选或文本 |
| 产品类型 | 单选或文本 |
| 识别意图 | 文本 |
| 知识库命中 | 单选或文本 |
| 会话ID | 文本 |
| 用户ID | 文本 |
| Bot回复摘要 | 多行文本 |
| 收录时间 | 文本 |

如果启用 `KITTY_CSBOT_FEEDBACK_HOOK=1`，同一张表还需要：`反馈时间`、`反馈来源`、`反馈内容`、`问题类型`。建议先在测试表启用 hook，确认字段类型和权限后再指向正式表。

## 3. 配置环境变量

复制模板：

```bash
cp kitty-runtime/.env.production.example .env.production
```

必须填写：

- 模型：`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；
- 飞书：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`；
- 多维表：`BITABLE_APP_TOKEN`、`BITABLE_TABLE_ID`；
- 内部通知：`INTERNAL_DEBUG_CHAT_ID`、`FEEDBACK_BOT_USER_ID`。

可靠性参数已有安全默认值，通常不需要修改：

```text
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=2
CS_TOOL_TIMEOUT_SECONDS=30
CS_FEEDBACK_TIMEOUT_SECONDS=15
KITTY_DELIVERY_MAX_ATTEMPTS=5
KITTY_DELIVERY_RETRY_BASE_SECONDS=1
```

不要把 `.env.production` 提交到 Git。模板里的 `replace-me` 只用于说明，不能用于生产。

## 4. Docker 部署

从仓库根目录执行：

```bash
docker build -t cs-bot-kitty:local -f kitty-runtime/Dockerfile .
docker run -d --name cs-bot-kitty \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env.production \
  -v kitty-data:/data/kitty \
  cs-bot-kitty:local
```

在云平台部署时也必须把 `/data/kitty` 挂载到持久卷。当前版本使用 SQLite，会话、事件幂等和 CS-bot 数据共用本地持久卷，因此副本数保持为 1。

## 5. 域名与网关

- 对外提供有效 HTTPS 证书；
- 将 `/feishu/events` 反向代理到容器 8000 端口；
- 保留原始请求体和 `X-Lark-*` 请求头，不能在网关层重写 JSON；
- 使用 `/health` 做 liveness probe；
- 使用 `/ready` 做 readiness probe；
- 限制请求体大小，应用默认上限是 1 MiB；
- 生产环境默认关闭 `/v1/messages`。只有设置独立的 `KITTY_DEBUG_API_TOKEN` 后才启用，并要求 `Authorization: Bearer <token>` 或 `X-Kitty-API-Token`；即使启用，也建议在网关层限制为内网访问。

`/ready` 会返回投递队列统计。监控应在 `dead > 0` 时告警；这类任务通常意味着飞书权限、目标群、应用发布状态或凭据存在永久错误。

查看和重放失败任务：

```bash
docker exec cs-bot-kitty kitty --state-dir /data/kitty --delivery-status
docker exec cs-bot-kitty kitty --state-dir /data/kitty --retry-delivery om_xxx
docker restart cs-bot-kitty
```

## 6. 上线验收

按顺序验证：

1. `GET /health` 返回 200 和 `status=alive`；
2. `GET /ready` 返回 200、`status=ready`、`agent_mode=csbot`；
3. 飞书保存事件订阅 URL 时 challenge 校验通过；
4. 单聊发送一个知识库问题，机器人能回复；
5. 群聊不 @ 时不回复，@ 后正常回复；
6. 重复投递同一 `message_id` 时只处理一次；
7. 发送图片时收到“请用文字描述”的提示；
8. 提交一条反馈，确认多维表字段正确；
9. 重启容器后继续对话，确认历史和去重记录仍存在；
10. 检查日志中没有密钥、完整 token 或用户敏感内容泄漏。
11. 临时让飞书发送失败一次，确认任务自动重试且群里只出现一条回复。

完成这些检查后，才建议把机器人加入正式群聊。
