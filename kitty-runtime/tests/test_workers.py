import asyncio
import tempfile
import unittest
from pathlib import Path

from kitty.agent.providers.base import ModelResponse
from kitty.agent.providers.mock import MockProvider
from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, RecordMeta
from kitty.runtime import KittyRuntime


class ConcurrencyProvider:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def complete(self, messages, tools):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.03)
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        self.active -= 1
        return ModelResponse(content=f"done:{user}")


class WorkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.record = AgentRecord(
            user_id="ou_test",
            chat_id="oc_test",
            meta=RecordMeta(title="Test group", channel="test"),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def config(self):
        return KittyConfig(state_dir=self.state_dir, stream_chunk_size=4)

    async def test_dispatch_emits_compatible_events_and_persists_history(self):
        runtime = KittyRuntime(config=self.config(), provider=MockProvider(prefix="echo:"))
        observed = []

        async def capture(event, ctx):
            observed.append(event)

        runtime.hooks.register(capture, name="capture")
        try:
            first = await runtime.dispatch("session-1", "hello", record=self.record)
            second = await runtime.dispatch("session-1", "again", record=self.record)
            state = runtime.store.load("session-1")
        finally:
            await runtime.close()

        self.assertEqual(first.reply, "echo:hello")
        self.assertEqual(second.reply, "echo:again")
        self.assertEqual([m["role"] for m in state.messages], ["user", "assistant", "user", "assistant"])
        types = [event.event_type for event in observed]
        self.assertIn("worker.started", types)
        self.assertEqual(types.count("cli.turn_done"), 2)
        wire_types = [
            event.data["wire"]["wire_type"]
            for event in observed
            if event.event_type == "cli.wire"
        ]
        self.assertIn("TurnBegin", wire_types)
        self.assertIn("TextPart", wire_types)
        self.assertIn("TurnEnd", wire_types)

    async def test_same_session_serializes_and_different_sessions_overlap(self):
        provider = ConcurrencyProvider()
        runtime = KittyRuntime(config=self.config(), provider=provider)
        try:
            await asyncio.gather(
                runtime.dispatch("same", "one", record=self.record),
                runtime.dispatch("same", "two", record=self.record),
            )
            self.assertEqual(provider.max_active, 1)

            provider.max_active = 0
            await asyncio.gather(
                runtime.dispatch("left", "one", record=self.record),
                runtime.dispatch("right", "two", record=self.record),
            )
            self.assertGreaterEqual(provider.max_active, 2)
        finally:
            await runtime.close()

    async def test_session_recovers_after_runtime_restart(self):
        runtime = KittyRuntime(config=self.config(), provider=MockProvider())
        await runtime.dispatch("durable", "first", record=self.record)
        await runtime.close()

        seen_history_lengths = []

        async def responder(messages, tools):
            seen_history_lengths.append(len(messages))
            return ModelResponse(content="restored")

        restarted = KittyRuntime(config=self.config(), provider=MockProvider(responder=responder))
        try:
            result = await restarted.dispatch("durable", "second", record=self.record)
        finally:
            await restarted.close()

        self.assertEqual(result.reply, "restored")
        self.assertGreaterEqual(seen_history_lengths[0], 4)

    async def test_broken_hook_does_not_break_reply(self):
        runtime = KittyRuntime(config=self.config(), provider=MockProvider())

        async def broken(event, ctx):
            raise RuntimeError("hook failure")

        runtime.hooks.register(broken, name="broken")
        try:
            result = await runtime.dispatch("safe", "hello", record=self.record)
        finally:
            await runtime.close()
        self.assertIn("hello", result.reply)
