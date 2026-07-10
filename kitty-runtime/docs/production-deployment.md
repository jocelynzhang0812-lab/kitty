# 飞书生产部署指南

## 1. 创建飞书自建应用

1. 创建企业自建应用并启用机器人能力；
2. 申请并发布所需权限：
   - 单聊消息：`im:message.p2p_msg` 或对应只读权限；
   - 群聊 @ 消息：`im:message.group_at_msg` 或对应只读权限；
   - 机器人发送：`im:message:send_as_bot`；
3. 订阅 `im.message.receive_v1`；
4. 选择“将事件发送至开发者服务器”；
5. 设置 Verification Token 和 Encrypt Key；
6. 发布应用并把机器人加入目标群聊。

官方参考：

- [开发者服务器订阅模式](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server?lang=zh-CN)
- [Encrypt Key 加密和签名](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case)
- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)

## 2. 配置机器人

```bash
cp kitty-runtime/.env.production.example .env.production
```

必须填写：

- `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`；
- `FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`；
- `KITTY_SYSTEM_PROMPT`。

可选扩展：

```text
KITTY_TOOL_MODULES=examples.tools,my_bot.tools
KITTY_HOOK_PATHS=examples/echo_hook.py,my_bot/audit_hook.py
```

工具模块必须提供同步的 `register_tools(registry)`。Hook 路径相对于 `KITTY_PROJECT_ROOT`。

## 3. Docker 部署

```bash
docker build -t kitty -f kitty-runtime/Dockerfile .
docker run -d --name kitty \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env.production \
  -v kitty-data:/data/kitty \
  kitty
```

飞书回调地址：

```text
https://你的域名/feishu/events
```

## 4. 网关与监控

- 保留原始请求体和所有 `X-Lark-*` 请求头；
- `/health` 用于 liveness；
- `/ready` 用于 readiness；
- 不要直接暴露 `/v1/messages`；
- `delivery.dead > 0` 时告警；
- `/data/kitty` 必须使用持久卷；
- 当前副本数保持为 1。

查看和重放失败任务：

```bash
docker exec kitty kitty --state-dir /data/kitty --delivery-status
docker exec kitty kitty --state-dir /data/kitty --retry-delivery om_xxx
docker restart kitty
```

## 5. 上线验收

1. `/health` 和 `/ready` 返回 200；
2. 飞书保存事件 URL 时 challenge 通过；
3. 单聊发送文字，机器人正常回复；
4. 群聊不 @ 时不回复，@ 后回复；
5. 非文字消息收到文字引导；
6. 重复 `message_id` 只处理一次；
7. 模拟一次发送失败，确认自动重试且只出现一条回复；
8. 重启容器后会话和待投递任务继续存在；
9. 日志不包含模型或飞书密钥。
