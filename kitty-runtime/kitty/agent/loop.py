from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kitty.agent.providers.base import ModelProvider
from kitty.core.events import WireType
from kitty.skills.loader import SkillCatalog
from kitty.tools.registry import ToolRegistry


WireEmitter = Callable[[WireType, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class AgentRunResult:
    reply: str
    messages: list[dict[str, Any]]
    steps: int


class AgentLoop:
    """Provider-neutral tool calling loop with observable wire events."""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        system_prompt: str,
        max_steps: int = 8,
        stream_chunk_size: int = 80,
        skills: SkillCatalog | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.stream_chunk_size = stream_chunk_size
        self.skills = skills

    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        emit_wire: WireEmitter,
    ) -> AgentRunResult:
        system_prompt = self.system_prompt
        if self.skills is not None:
            selected = self.skills.select(user_message)
            if selected:
                system_prompt += "\n\nRelevant skills:\n" + self.skills.render_context(selected)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *[dict(item) for item in history],
            {"role": "user", "content": user_message},
        ]
        persisted: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        reply_parts: list[str] = []

        for step in range(1, self.max_steps + 1):
            response = await self.provider.complete(messages, self.tools.schemas())
            assistant: dict[str, Any] = {"role": "assistant", "content": response.content}
            if response.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in response.tool_calls
                ]
            messages.append(assistant)
            persisted.append(assistant)

            if response.content:
                reply_parts.append(response.content)
                for offset in range(0, len(response.content), self.stream_chunk_size):
                    await emit_wire(
                        WireType.TEXT_PART,
                        {"text": response.content[offset : offset + self.stream_chunk_size]},
                    )

            if not response.tool_calls:
                return AgentRunResult("".join(reply_parts), persisted, step)

            for call in response.tool_calls:
                await emit_wire(
                    WireType.TOOL_CALL,
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                )
                execution = await self.tools.execute(call.name, call.arguments)
                payload = execution.model_payload()
                await emit_wire(
                    WireType.TOOL_RESULT,
                    {"id": call.id, "name": call.name, "result": payload},
                )
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(payload, ensure_ascii=False),
                }
                messages.append(tool_message)
                persisted.append(tool_message)

        fallback = "Agent stopped after reaching the maximum number of steps."
        await emit_wire(WireType.TEXT_PART, {"text": fallback})
        persisted.append({"role": "assistant", "content": fallback})
        reply_parts.append(fallback)
        return AgentRunResult("".join(reply_parts), persisted, self.max_steps)
