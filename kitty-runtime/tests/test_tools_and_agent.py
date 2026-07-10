import unittest
import time

from kitty.agent.loop import AgentLoop
from kitty.agent.providers.base import ModelResponse, ModelToolCall
from kitty.tools.registry import ToolRegistry


class ScriptedProvider:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=(ModelToolCall("call-1", "add", {"a": 2, "b": 3}),)
            )
        return ModelResponse(content="The result is 5.")


class ToolAndAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_loop_emits_wire_events(self):
        registry = ToolRegistry()
        registry.add(
            "add",
            lambda a, b: a + b,
            parameters={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        )
        provider = ScriptedProvider()
        loop = AgentLoop(
            provider,
            registry,
            system_prompt="test",
            max_steps=3,
            stream_chunk_size=5,
        )
        events = []

        async def emit(kind, payload):
            events.append((kind.value, payload))

        result = await loop.run("add", [], emit)

        self.assertEqual(result.reply, "The result is 5.")
        self.assertEqual(provider.calls, 2)
        self.assertIn("ToolCall", [kind for kind, _ in events])
        self.assertIn("ToolResult", [kind for kind, _ in events])
        tool_result = next(payload for kind, payload in events if kind == "ToolResult")
        self.assertEqual(tool_result["result"]["output"], 5)
        tool_call_message = next(message for message in result.messages if "tool_calls" in message)
        self.assertEqual(tool_call_message["tool_calls"][0]["type"], "function")
        self.assertEqual(
            tool_call_message["tool_calls"][0]["function"]["arguments"],
            '{"a": 2, "b": 3}',
        )

    async def test_allowlist_and_validation(self):
        registry = ToolRegistry(allowlist=["safe"])
        registry.add(
            "safe",
            lambda value: value,
            parameters={"type": "object", "properties": {"value": {}}, "required": ["value"]},
        )
        registry.add("blocked", lambda: True)

        missing = await registry.execute("safe", {})
        blocked = await registry.execute("blocked", {})

        self.assertFalse(missing.ok)
        self.assertIn("missing required", missing.error)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error, "tool is not allowed")

    async def test_blocking_sync_tool_is_timed_out_off_loop(self):
        registry = ToolRegistry(default_timeout_seconds=0.01)

        def blocking():
            time.sleep(0.1)
            return True

        registry.add("blocking", blocking)
        started = time.monotonic()
        result = await registry.execute("blocking")
        elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertLess(elapsed, 0.08)
