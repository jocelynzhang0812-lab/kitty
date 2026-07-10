# CS-bot compatibility

The compatibility boundary is intentionally explicit: Kitty owns workers,
events, hooks, model orchestration, and durable sessions; CS-bot owns customer
service tools and business integrations.

Once the CS-bot Python dependencies are installed, an application can bridge
its registered tools into Kitty:

```python
from pathlib import Path

import csbot
from csbot.agent.core import ToolRegistry as CSBotToolRegistry
from kitty.runtime import KittyRuntime

repo_root = Path(__file__).resolve().parent
runtime = KittyRuntime(project_root=repo_root)
count = runtime.import_csbot_tools(CSBotToolRegistry)
runtime.load_hook(repo_root / "csbot/hooks/feedback_hook.py")
```

The hook receives the fields it currently consumes:

- `event.event_type`, `event.session_id`, and `event.data`;
- `ctx.record`, including user and metadata fields;
- `ctx.work_dir`, `ctx.session_id`, and `ctx.logger`;
- `TurnBegin`, `TextPart`, and `cli.turn_done` lifecycle events.

## Current repository limitation

The CS-bot repository does not currently declare its Python dependencies in a
`requirements.txt` or `pyproject.toml`. Loading the real feedback hook therefore
depends on packages already present in the deployment environment, including
its Feishu HTTP client dependencies. Kitty's contract tests use a hook with the
same observable interface and do not silently install or vendor CS-bot's
dependencies.

The real feedback hook performs outbound Feishu/Bitable work on
`cli.turn_done`. Use mock integrations during local testing to avoid external
writes.
