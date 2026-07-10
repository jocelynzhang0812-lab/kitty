# Kitty Runtime Compatibility Project

This directory contains a clean-room implementation of the Kitty interfaces
that can be inferred from the surrounding CS-bot repository. It is not the
original Kitty source code.

The first milestone provides:

- one serial worker queue per session and concurrency across sessions;
- durable SQLite session history;
- `cli.wire` and `cli.turn_done` lifecycle events;
- dynamically loaded Python hooks with failure and timeout isolation;
- provider-neutral agent/tool loop;
- OpenAI-compatible Chat Completions provider;
- deterministic mock model mode;
- `SKILL.md` discovery under `.agents/*/skills/*`;
- read-only `AGENTS.md` and `MEMORY.md` context loading;
- a local CLI, dependency-free ASGI API, Feishu event parser, and JSONL events.

## Run locally

No third-party dependency is required for mock mode.

```bash
cd kitty-runtime
env PYTHONPYCACHEPREFIX=.pycache python3 -m kitty --once "hello"
```

Interactive mode with an example hook:

```bash
env PYTHONPYCACHEPREFIX=.pycache python3 -m kitty \
  --session demo \
  --hook examples/echo_hook.py \
  --json-events
```

State is written under `.kitty/` by default:

```text
.kitty/
├── sessions.db
├── logs/
│   └── worker_<session>.log
└── workspaces/
```

## HTTP and Feishu event API

The ASGI app itself has no third-party dependency. Run it with an ASGI server
such as Uvicorn when one is available:

```bash
uvicorn kitty.server:create_app --factory --host 127.0.0.1 --port 8000
```

Endpoints:

- `GET /health`
- `POST /v1/messages`
- `POST /feishu/events`

`/feishu/events` supports URL challenge responses, text-message parsing,
mention checks, and message-ID deduplication. A production deployment should
pass an outbound `reply_sender` to `KittyASGIApp`. The default factory creates a
dependency-free `FeishuSender` when `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are
present; otherwise it returns the generated reply only in its HTTP response for
local verification.

When an outbound sender is configured, the webhook acknowledges the event
immediately and processes the model turn in a tracked background task, avoiding
Feishu callback timeouts.

## Real model provider

Mock mode is the default. Applications can construct an
`OpenAICompatibleProvider` with a runtime-only API key:

```python
import os

from kitty.agent.providers import OpenAICompatibleProvider
from kitty.runtime import KittyRuntime

provider = OpenAICompatibleProvider(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
    model=os.environ["LLM_MODEL"],
)
runtime = KittyRuntime(provider=provider)
```

## Test

```bash
env PYTHONPYCACHEPREFIX=.pycache python3 -m unittest discover -s tests -v
```

## Compatibility boundary

The implemented hook contract matches the signatures used by
`csbot/hooks/feedback_hook.py`:

```python
listened_events = ["cli.wire", "cli.turn_done"]

async def hook(event, ctx):
    ...
```

The runtime exposes `event.event_type`, `event.session_id`, `event.data`,
`ctx.record`, `ctx.work_dir`, `ctx.session_id`, and `ctx.logger`.

Python hooks execute code in the runtime process and must therefore be trusted.

When `FEISHU_VERIFICATION_TOKEN` is configured, the ASGI factory rejects event
payloads with a different token. Encrypted Feishu events are not implemented in
this milestone.

See `docs/architecture.md` and `docs/event-protocol.md` for the inferred
contracts and explicit uncertainty boundaries. CS-bot bridging and its current
dependency limitation are documented in `docs/csbot-compatibility.md`.
