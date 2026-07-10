# Kitty 事件协议 v1

每个内部事件包含：

```json
{
  "event_id": "unique-id",
  "event_type": "cli.wire",
  "session_id": "oc_example",
  "timestamp": 0.0,
  "data": {}
}
```

## Turn 生命周期

1. `worker.started`；
2. `cli.wire` / `TurnBegin`；
3. 零个或多个 `TextPart`、`ToolCall`、`ToolResult`、`ContentPart`；
4. `cli.wire` / `TurnEnd`；
5. `cli.turn_done`；
6. `worker.stopped`。

Hook 接收 `event` 和 `ctx`：

- `event.event_type`、`event.session_id`、`event.data`；
- `ctx.record`、`ctx.work_dir`、`ctx.session_id`、`ctx.logger`。

## 飞书入口

`POST /feishu/events` 按顺序执行：

1. 使用原始请求体校验 `X-Lark-Signature`；
2. 拒绝超过允许时钟偏差的请求；
3. 使用 `SHA256(Encrypt Key)` 解密 AES-256-CBC payload；
4. 校验 PKCS#7 padding 和 Verification Token；
5. 解析文字、单聊/群聊和 @ 规则；
6. 使用 `message_id` 创建持久化投递任务。

## 投递状态

```text
pending -> processing -> completed
                      -> pending (retry)
                      -> dead
```

模型回复在发送前保存，发送重试使用由 `message_id` 派生的稳定飞书 `uuid`。任务失败按指数退避重试，服务重启会恢复 `pending` 和中断的 `processing` 任务。

## 失败语义

- Hook 和工具失败被隔离在当前调用；
- 模型失败只重试当前持久化任务；
- 同一 `request_id` 的已完成 turn 返回缓存结果；
- 关闭时等待后台投递，超时任务回到 `pending`。
