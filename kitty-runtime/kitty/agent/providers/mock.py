from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from kitty.agent.providers.base import ModelResponse


MockResponder = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    ModelResponse | str | Awaitable[ModelResponse | str],
]


class MockProvider:
    """Deterministic provider for local demos and runtime tests."""

    def __init__(self, responder: MockResponder | None = None, prefix: str = "Mock reply: "):
        self.responder = responder
        self.prefix = prefix

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        if self.responder is not None:
            value = self.responder(messages, tools)
            if inspect.isawaitable(value):
                value = await value
            return value if isinstance(value, ModelResponse) else ModelResponse(content=str(value))

        last_user = next(
            (str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        return ModelResponse(content=f"{self.prefix}{last_user}")
