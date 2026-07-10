# Kitty 兼容事件协议 v0.2

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

正常顺序为：

1. `worker.started`，worker 首次启动；
2. `cli.wire` / `TurnBegin`；
3. 零个或多个 `TextPart`、`ToolCall`、`ToolResult` 或 `ContentPart`；
4. `cli.wire` / `TurnEnd`；
5. `cli.turn_done`；
6. `worker.stopped`，worker 关闭。

`TurnBegin` 保留现有 CS-bot hook 使用的数据结构：

```json
{
  "wire": {
    "wire_type": "TurnBegin",
    "user_input": "hello",
    "user_id": "ou_example",
    "request_id": "request-id"
  }
}
```

## 飞书入口安全

`POST /feishu/events` 在解析业务 JSON 前依次执行：

1. 使用时间戳、nonce、Encrypt Key 和原始请求体计算 SHA-256 签名；
2. 使用常量时间比较校验 `X-Lark-Signature`；
3. 拒绝超过允许时间偏差的请求，默认 300 秒；
4. 使用 `SHA256(Encrypt Key)` 作为 AES-256-CBC 密钥解密 `encrypt` 字段；
5. 校验 PKCS#7 padding 和 Verification Token；
6. 使用飞书 `message_id` 做持久化幂等去重。

单聊文字消息不要求 @；群聊默认只响应明确 @ 机器人的消息。可用 `FEISHU_REQUIRE_MENTION=0` 放宽群聊规则，但不建议为公开群默认关闭。

## 失败语义

- hook 失败会被记录，但不终止当前 turn；
- 模型或 worker 失败只影响当前请求，worker 可继续接收后续请求；
- 工具错误以结构化结果返回给模型，不穿透 worker 边界；
- 飞书回调先快速确认，模型处理和回复在受跟踪的后台任务中完成；
- 服务关闭会等待后台任务最多 10 秒，然后取消仍未完成的任务。
