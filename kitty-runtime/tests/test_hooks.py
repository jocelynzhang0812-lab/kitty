import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path

from kitty.core.context import AgentRecord, HookContext, RecordMeta
from kitty.core.events import SessionEvent
from kitty.hooks.bus import HookBus
from kitty.hooks.loader import load_hook


class HookBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp.name)
        self.ctx = HookContext(
            record=AgentRecord(user_id="u1", meta=RecordMeta(title="Test")),
            work_dir=self.work_dir,
            session_id="s1",
            logger=logging.getLogger("test-hooks"),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_filters_events_and_isolates_failure(self):
        calls = []
        bus = HookBus(default_timeout_seconds=0.1)

        async def good(event, ctx):
            calls.append(event.event_type)

        async def broken(event, ctx):
            raise RuntimeError("boom")

        bus.register(good, listened_events=["cli.wire"], name="good")
        bus.register(broken, listened_events=["cli.wire"], name="broken")

        ignored = await bus.emit(SessionEvent.create("worker.started", "s1"), self.ctx)
        results = await bus.emit(SessionEvent.create("cli.wire", "s1"), self.ctx)

        self.assertEqual(ignored, [])
        self.assertEqual(calls, ["cli.wire"])
        self.assertEqual([item.ok for item in results], [True, False])
        self.assertIn("RuntimeError", results[1].error)

    async def test_times_out_slow_hook(self):
        bus = HookBus(default_timeout_seconds=0.01)

        async def slow(event, ctx):
            await asyncio.sleep(0.1)

        bus.register(slow, name="slow")
        result = await bus.emit(SessionEvent.create("cli.wire", "s1"), self.ctx)
        self.assertTrue(result[0].timed_out)

    async def test_times_out_blocking_sync_hook_without_blocking_worker(self):
        bus = HookBus(default_timeout_seconds=0.01)

        def blocking(event, ctx):
            time.sleep(0.1)

        bus.register(blocking, name="blocking")
        started = time.monotonic()
        result = await bus.emit(SessionEvent.create("cli.wire", "s1"), self.ctx)
        elapsed = time.monotonic() - started

        self.assertTrue(result[0].timed_out)
        self.assertLess(elapsed, 0.08)

    async def test_loads_kitty_style_hook_module(self):
        bus = HookBus()
        fixture = Path(__file__).parent / "fixtures" / "record_hook.py"
        loaded = load_hook(fixture, bus)

        await bus.emit(SessionEvent.create("worker.started", "s1"), self.ctx)
        await bus.emit(SessionEvent.create("cli.turn_done", "s1"), self.ctx)

        self.assertEqual(len(loaded.module.events), 1)
        self.assertEqual(loaded.module.events[0]["title"], "Test")
