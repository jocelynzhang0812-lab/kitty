# Kitty compatibility event protocol v0.1

Every event contains:

```json
{
  "event_id": "unique-id",
  "event_type": "cli.wire",
  "session_id": "oc_example",
  "timestamp": 0.0,
  "data": {}
}
```

## Turn lifecycle

The normal order is:

1. `worker.started`, once for a live worker;
2. `cli.wire` / `TurnBegin`;
3. zero or more `TextPart`, `ToolCall`, `ToolResult`, or `ContentPart` events;
4. `cli.wire` / `TurnEnd`;
5. `cli.turn_done`;
6. `worker.stopped`, when the worker is shut down.

`TurnBegin` preserves the shape consumed by the existing CS-bot hook:

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

Text output is streamed as:

```json
{
  "wire": {
    "wire_type": "TextPart",
    "text": "partial output",
    "request_id": "request-id"
  }
}
```

## Failure semantics

- Hook failures are recorded and do not stop the turn.
- Model or worker failures produce `worker.failed` and fail only the current
  request; the session worker continues accepting later requests.
- Tool errors are returned to the model as structured results rather than
  raising through the worker boundary.
