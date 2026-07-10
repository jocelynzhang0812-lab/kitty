from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kitty.core.context import HookContext
from kitty.core.events import SessionEvent


HookCallback = Callable[[SessionEvent, HookContext], Awaitable[None] | None]


@dataclass(slots=True)
class HookBinding:
    name: str
    callback: HookCallback
    listened_events: frozenset[str]
    timeout_seconds: float

    def accepts(self, event_type: str) -> bool:
        return not self.listened_events or event_type in self.listened_events


@dataclass(slots=True, frozen=True)
class HookExecution:
    hook_name: str
    event_type: str
    ok: bool
    error: str = ""
    timed_out: bool = False


class HookBus:
    """Failure-isolated asynchronous event dispatcher."""

    def __init__(self, default_timeout_seconds: float = 10.0):
        self.default_timeout_seconds = default_timeout_seconds
        self._bindings: list[HookBinding] = []

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return tuple(self._bindings)

    def register(
        self,
        callback: HookCallback,
        *,
        listened_events: Iterable[str] | None = None,
        name: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookBinding:
        hook_name = name or getattr(callback, "__qualname__", repr(callback))
        if any(binding.name == hook_name for binding in self._bindings):
            raise ValueError(f"hook already registered: {hook_name}")
        binding = HookBinding(
            name=hook_name,
            callback=callback,
            listened_events=frozenset(str(item) for item in (listened_events or ())),
            timeout_seconds=timeout_seconds or self.default_timeout_seconds,
        )
        self._bindings.append(binding)
        return binding

    async def emit(self, event: SessionEvent, ctx: HookContext) -> list[HookExecution]:
        selected = [binding for binding in self._bindings if binding.accepts(event.event_type)]
        if not selected:
            return []
        return list(await asyncio.gather(*(self._execute(binding, event, ctx) for binding in selected)))

    async def _execute(
        self,
        binding: HookBinding,
        event: SessionEvent,
        ctx: HookContext,
    ) -> HookExecution:
        try:
            async def invoke() -> None:
                if inspect.iscoroutinefunction(binding.callback):
                    await binding.callback(event, ctx)
                    return
                value: Any = await asyncio.to_thread(binding.callback, event, ctx)
                if inspect.isawaitable(value):
                    await value

            await asyncio.wait_for(invoke(), timeout=binding.timeout_seconds)
            return HookExecution(binding.name, event.event_type, True)
        except TimeoutError:
            message = f"hook timed out after {binding.timeout_seconds}s"
            ctx.logger.warning("[%s] %s", binding.name, message)
            return HookExecution(binding.name, event.event_type, False, message, True)
        except Exception as exc:  # Hook failures must not stop the worker.
            message = f"{type(exc).__name__}: {exc}"
            ctx.logger.exception("Hook %s failed for %s", binding.name, event.event_type)
            return HookExecution(binding.name, event.event_type, False, message)
