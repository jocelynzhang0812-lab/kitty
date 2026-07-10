# Inferred Kitty architecture

This implementation treats Kitty as a host runtime and CS-bot as a domain
agent installed on top of it.

```text
Channel adapter
    -> WorkerManager
        -> SessionWorker (serialized queue)
            -> AgentLoop
                -> ModelProvider
                -> ToolRegistry
                -> SkillCatalog
            -> HookBus
            -> SQLiteSessionStore
```

## Confirmed compatibility evidence

The CS-bot repository exposes these observable integration contracts:

- worker-specific logs named like `worker_oc_*.log`;
- Python event hooks configured on a worker or session;
- `cli.wire` and `cli.turn_done` events;
- `TurnBegin`, `TextPart`, and `ContentPart` wire payloads;
- hook context fields `record`, `work_dir`, `session_id`, and `logger`;
- skills stored below `.agents/<agent>/skills/<skill>/SKILL.md`;
- file-backed long-term memory and learning directories.

## Deliberate implementation choices

- Workers are in-process asyncio actors in v0.1. Their public boundary allows a
  later subprocess or container implementation.
- SQLite persists session history. Human-curated `MEMORY.md` mutation is not
  automated in v0.1.
- Model access is represented by a provider protocol. Only deterministic mock
  mode is enabled by default; an OpenAI-compatible provider is included.
- Hooks run concurrently for each event, with independent timeout and failure
  handling. They cannot crash the owning worker.
- Tools are allow-listable and receive per-call timeouts. Arbitrary shell
  execution is intentionally absent.
- The dependency-free ASGI layer exposes health, debug chat, and Feishu event
  routes. Production outbound delivery remains an injected channel concern.

## Not inferred

The repository does not establish whether the original Kitty used queues,
containers, a remote scheduler, a specific model gateway, or a particular
authorization system. This project does not claim compatibility with those
unknown internals.
