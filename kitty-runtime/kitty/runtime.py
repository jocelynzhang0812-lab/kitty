from __future__ import annotations

from pathlib import Path
import asyncio
import inspect
from typing import Any

from kitty.agent.handler import TurnHandler
from kitty.agent.loop import AgentLoop
from kitty.agent.providers.base import ModelProvider
from kitty.agent.providers.mock import MockProvider
from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord
from kitty.hooks.bus import HookBus
from kitty.hooks.loader import LoadedHook, load_hook
from kitty.memory.file_context import FileContext
from kitty.memory.session_store import SQLiteSessionStore
from kitty.skills.loader import SkillCatalog
from kitty.tools.registry import ToolRegistry
from kitty.workers.manager import WorkerManager
from kitty.workers.worker import WorkerResult


class KittyRuntime:
    """Facade wiring providers, workers, hooks, tools, skills, and storage."""

    def __init__(
        self,
        config: KittyConfig | None = None,
        *,
        provider: ModelProvider | None = None,
        project_root: str | Path | None = None,
        tools: ToolRegistry | None = None,
        hooks: HookBus | None = None,
        skills: SkillCatalog | None = None,
        turn_handler: TurnHandler | None = None,
    ):
        self.config = config or KittyConfig.from_env()
        self.config.ensure_dirs()
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self.provider = provider or MockProvider()
        self.tools = tools or ToolRegistry(
            default_timeout_seconds=self.config.tool_timeout_seconds
        )
        self.hooks = hooks or HookBus(self.config.hook_timeout_seconds)
        self.skills = skills if skills is not None else SkillCatalog.discover(self.project_root)
        self.file_context = FileContext.load(self.project_root)
        self.store = SQLiteSessionStore(self.config.session_db_path)
        system_prompt = self.config.system_prompt
        rendered_file_context = self.file_context.render()
        if rendered_file_context:
            system_prompt += "\n\n" + rendered_file_context
        self.agent: TurnHandler = turn_handler or AgentLoop(
            self.provider,
            self.tools,
            system_prompt=system_prompt,
            max_steps=self.config.max_agent_steps,
            stream_chunk_size=self.config.stream_chunk_size,
            skills=self.skills,
        )
        self.workers = WorkerManager(
            agent=self.agent,
            hooks=self.hooks,
            store=self.store,
            workspace_root=self.config.workspace_root,
            log_dir=self.config.log_dir,
        )
        self.loaded_hooks: list[LoadedHook] = []
        self._started = False
        self._start_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            startup = getattr(self.agent, "startup", None)
            if callable(startup):
                value = startup()
                if inspect.isawaitable(value):
                    await value
            self._started = True

    def load_hook(self, path: str | Path) -> LoadedHook:
        loaded = load_hook(path, self.hooks)
        self.loaded_hooks.append(loaded)
        return loaded

    def import_csbot_tools(self, registry: Any) -> int:
        return self.tools.import_csbot_registry(registry)

    async def dispatch(
        self,
        session_id: str,
        message: str,
        *,
        record: AgentRecord | None = None,
        request_id: str | None = None,
    ) -> WorkerResult:
        await self.start()
        return await self.workers.dispatch(
            session_id,
            message,
            record=record,
            request_id=request_id,
        )

    async def close(self) -> None:
        await self.workers.shutdown()
        shutdown = getattr(self.agent, "shutdown", None)
        if self._started and callable(shutdown):
            value = shutdown()
            if inspect.isawaitable(value):
                await value
        self._started = False

    async def __aenter__(self) -> "KittyRuntime":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
