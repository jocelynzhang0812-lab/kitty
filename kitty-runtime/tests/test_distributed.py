import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from kitty.agent.providers.mock import MockProvider
from kitty.channels.base import ChannelMessage
from kitty.channels.codec import serialize_channel_message
from kitty.channels.feishu import FeishuEventParser
from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, RecordMeta
from kitty.distributed.config import DistributedSettings
from kitty.distributed.ingress import DistributedIngressApp
from kitty.distributed.sender import SenderService
from kitty.distributed.worker import AgentWorkerService
from kitty.memory.postgres_store import LeaseLostError, PostgresStore
from kitty.memory.session_store import SessionState
from kitty.runtime import KittyRuntime


DATABASE_URL = os.getenv("KITTY_TEST_POSTGRES_URL", "")


def payload(job_id: str, session_id: str = "oc_1"):
    return serialize_channel_message(
        ChannelMessage(
            session_id=session_id,
            content=f"hello {job_id}",
            request_id=job_id,
            record=AgentRecord(
                user_id="ou_1",
                chat_id=session_id,
                meta=RecordMeta(title="Feishu", channel="feishu"),
            ),
        )
    )


def settings(worker_id: str, *, retry_base: float = 0.01):
    return DistributedSettings(
        database_url=DATABASE_URL,
        worker_id=worker_id,
        poll_interval_seconds=0.01,
        lease_seconds=5,
        concurrency=1,
        max_attempts=3,
        retry_base_seconds=retry_base,
    )


def feishu_event(message_id: str):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "mentions": [],
            },
        },
    }


async def asgi_request(app, method, path, payload=None):
    sent = []
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {
            "type": "http.request",
            "body": json.dumps(payload or {}).encode("utf-8"),
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = next(item["body"] for item in sent if item["type"] == "http.response.body")
    return status, json.loads(body)


@unittest.skipUnless(DATABASE_URL, "KITTY_TEST_POSTGRES_URL is not configured")
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = PostgresStore(DATABASE_URL)

    def setUp(self):
        self.store.clear_all()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_claims_jobs_once_and_skips_a_session_with_an_active_lease(self):
        self.store.enqueue_inbox("job-1", "session-a", payload("job-1", "session-a"))
        self.store.enqueue_inbox("job-2", "session-a", payload("job-2", "session-a"))
        self.store.enqueue_inbox("job-3", "session-b", payload("job-3", "session-b"))

        first = self.store.claim_inbox("worker-1", 5)
        token = self.store.acquire_session_lease("session-a", "worker-1", 5)
        second = self.store.claim_inbox("worker-2", 5)

        self.assertEqual(first.job_id, "job-1")
        self.assertIsNotNone(token)
        self.assertEqual(second.job_id, "job-3")
        self.assertEqual(second.attempts, 1)

    def test_expired_lease_is_reclaimed_and_fencing_blocks_stale_write(self):
        first_token = self.store.acquire_session_lease("session-a", "worker-1", 0.2)
        with self.store.session_write_lease("session-a", "worker-1", first_token):
            self.store.save(SessionState("session-a", metadata={"owner": "first"}))

        time.sleep(0.25)
        second_token = self.store.acquire_session_lease("session-a", "worker-2", 5)

        self.assertGreater(second_token, first_token)
        with self.assertRaises(LeaseLostError):
            with self.store.session_write_lease("session-a", "worker-1", first_token):
                self.store.save(SessionState("session-a", metadata={"owner": "stale"}))
        self.assertEqual(self.store.load("session-a").metadata["owner"], "first")

    def test_expired_inbox_and_outbox_jobs_are_reclaimed(self):
        self.store.enqueue_inbox("job-1", "session-a", payload("job-1", "session-a"))
        first = self.store.claim_inbox("worker-1", 0.1)
        first_token = self.store.acquire_session_lease("session-a", "worker-1", 0.1)
        time.sleep(0.15)
        recovered = self.store.claim_inbox("worker-2", 5)
        second_token = self.store.acquire_session_lease("session-a", "worker-2", 5)
        self.store.complete_inbox_with_outbox(
            recovered,
            "worker-2",
            second_token,
            "reply",
        )

        first_send = self.store.claim_outbox("sender-1", 0.1)
        time.sleep(0.15)
        recovered_send = self.store.claim_outbox("sender-2", 5)

        self.assertEqual(first.job_id, recovered.job_id)
        self.assertGreater(second_token, first_token)
        self.assertEqual(recovered.attempts, 2)
        self.assertEqual(first_send.job_id, recovered_send.job_id)
        self.assertEqual(recovered_send.attempts, 2)
        self.assertTrue(
            self.store.fail_outbox(recovered_send.job_id, "sender-2", "permanent")
        )
        self.assertTrue(self.store.requeue_dead("outbox", recovered_send.job_id))
        self.assertEqual(self.store.get_outbox(recovered_send.job_id)["status"], "pending")


@unittest.skipUnless(DATABASE_URL, "KITTY_TEST_POSTGRES_URL is not configured")
class DistributedPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = PostgresStore(DATABASE_URL)
        self.store.clear_all()
        self.temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def runtime(self, provider=None, suffix="worker"):
        config = KittyConfig(state_dir=Path(self.temp.name) / suffix)
        return KittyRuntime(
            config=config,
            provider=provider or MockProvider(prefix="distributed:"),
            store=self.store,
        )

    async def test_ingress_only_persists_and_acknowledges(self):
        app = DistributedIngressApp(self.store, FeishuEventParser())
        status, body = await asgi_request(
            app,
            "POST",
            "/feishu/events",
            feishu_event("job-ingress"),
        )

        inbox = self.store.get_inbox("job-ingress")
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(inbox["status"], "pending")
        self.assertIsNone(self.store.get_outbox("outbox:job-ingress"))

    async def test_agent_and_sender_are_separate_and_sender_retry_does_not_rerun_agent(self):
        self.store.enqueue_inbox("job-1", "oc_1", payload("job-1"))
        runtime = self.runtime()
        worker = AgentWorkerService(self.store, runtime, settings("agent"))

        self.assertTrue(await worker.run_once())
        self.assertEqual(self.store.get_inbox("job-1")["status"], "completed")
        self.assertEqual(self.store.get_outbox("outbox:job-1")["status"], "pending")
        self.assertEqual(len(self.store.load("oc_1").messages), 2)

        calls = []

        class FlakySender:
            async def send_text(self, chat_id, text, request_uuid):
                calls.append((chat_id, text, request_uuid))
                if len(calls) == 1:
                    raise RuntimeError("temporary")
                return {"code": 0}

        sender = SenderService(self.store, FlakySender(), settings("sender"))
        self.assertTrue(await sender.run_once())
        self.assertEqual(self.store.get_outbox("outbox:job-1")["status"], "pending")
        await asyncio.sleep(0.02)
        self.assertTrue(await sender.run_once())

        outbox = self.store.get_outbox("outbox:job-1")
        self.assertEqual(outbox["status"], "completed")
        self.assertEqual(outbox["attempts"], 2)
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(len(self.store.load("oc_1").messages), 2)
        await runtime.close()

    async def test_same_session_waits_while_another_worker_holds_the_lease(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_responder(messages, tools):
            started.set()
            await release.wait()
            return "done"

        self.store.enqueue_inbox("job-1", "oc_1", payload("job-1"))
        self.store.enqueue_inbox("job-2", "oc_1", payload("job-2"))
        runtime_one = self.runtime(MockProvider(responder=slow_responder), "one")
        runtime_two = self.runtime(MockProvider(prefix="second:"), "two")
        worker_one = AgentWorkerService(self.store, runtime_one, settings("agent-1"))
        worker_two = AgentWorkerService(self.store, runtime_two, settings("agent-2"))

        first = asyncio.create_task(worker_one.run_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        self.assertFalse(await worker_two.run_once())
        release.set()
        self.assertTrue(await first)
        self.assertTrue(await worker_two.run_once())

        self.assertEqual(len(self.store.load("oc_1").messages), 4)
        await runtime_one.close()
        await runtime_two.close()


if __name__ == "__main__":
    unittest.main()
