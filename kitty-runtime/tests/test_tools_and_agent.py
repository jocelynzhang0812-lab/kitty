import unittest
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kitty.agent.loop import AgentLoop
from kitty.agent.providers.base import ModelResponse, ModelToolCall
from kitty.core.config import KittyConfig
from kitty.runtime import KittyRuntime
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

    async def test_subprocess_executor_runs_importable_tool(self):
        registry = ToolRegistry(default_executor="subprocess")
        registry.add(
            "add",
            lambda a, b: a + b,
            handler_ref="tests.fixtures.tool_module:add",
        )

        result = await registry.execute("add", {"a": 2, "b": 4})

        self.assertTrue(result.ok)
        self.assertEqual(result.output, 6)

    async def test_subprocess_executor_handles_async_tools_and_stdout_noise(self):
        registry = ToolRegistry(default_executor="subprocess")
        registry.add("upper", lambda value: value, handler_ref="tests.fixtures.tool_module:async_upper")
        registry.add("noisy", lambda value: value, handler_ref="tests.fixtures.tool_module:noisy")

        upper = await registry.execute("upper", {"value": "kitty"})
        noisy = await registry.execute("noisy", {"value": "ok"})

        self.assertTrue(upper.ok)
        self.assertEqual(upper.output, "KITTY")
        self.assertTrue(noisy.ok)
        self.assertEqual(noisy.output, {"value": "ok"})

    async def test_subprocess_executor_times_out_and_kills_child(self):
        registry = ToolRegistry(default_timeout_seconds=0.05, default_executor="subprocess")
        registry.add("sleep", lambda seconds: seconds, handler_ref="tests.fixtures.tool_module:sleep_for")
        started = time.monotonic()

        result = await registry.execute("sleep", {"seconds": 1.0})
        elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertLess(elapsed, 0.5)

    async def test_subprocess_executor_requires_importable_handler(self):
        registry = ToolRegistry(default_executor="subprocess")
        registry.add("local", lambda value: value)

        result = await registry.execute("local", {"value": "x"})

        self.assertFalse(result.ok)
        self.assertIn("not subprocess-capable", result.error)

    async def test_tool_policy_denylist(self):
        registry = ToolRegistry(denylist=["blocked"])
        registry.add("blocked", lambda: True)
        registry.add("safe", lambda: True)

        result = await registry.execute("blocked")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "tool is denied by policy")
        self.assertEqual(
            [schema["function"]["name"] for schema in registry.schemas()],
            ["safe"],
        )

    async def test_runtime_reads_tool_executor_environment(self):
        with TemporaryDirectory() as tmpdir:
            env = {
                "KITTY_STATE_DIR": str(Path(tmpdir) / "state"),
                "KITTY_TOOL_EXECUTOR": "subprocess",
                "KITTY_TOOL_DENYLIST": "blocked, dangerous",
                "KITTY_TOOL_MAX_OUTPUT_BYTES": "2048",
            }
            with patch.dict("os.environ", env, clear=False):
                config = KittyConfig.from_env()
                runtime = KittyRuntime(config=config, project_root=Path.cwd())

        self.assertEqual(runtime.config.tool_executor, "subprocess")
        self.assertEqual(runtime.config.tool_denylist, ("blocked", "dangerous"))
        self.assertEqual(runtime.config.tool_max_output_bytes, 2048)
