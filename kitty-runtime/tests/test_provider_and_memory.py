import tempfile
import unittest
from pathlib import Path

from kitty.agent.providers.openai_compatible import OpenAICompatibleProvider
from kitty.memory.file_context import FileContext
from kitty.memory.session_store import SQLiteSessionStore


class ProviderAndMemoryTests(unittest.TestCase):
    def test_parses_openai_compatible_tool_call(self):
        response = OpenAICompatibleProvider.parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "lookup", "arguments": '{"q":"hello"}'},
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(response.tool_calls[0].name, "lookup")
        self.assertEqual(response.tool_calls[0].arguments, {"q": "hello"})

    def test_loads_project_guidance_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("agent rules", encoding="utf-8")
            (root / "MEMORY.md").write_text("long memory", encoding="utf-8")
            context = FileContext.load(root)
            rendered = context.render()
        self.assertIn("agent rules", rendered)
        self.assertIn("long memory", rendered)

    def test_event_dedupe_persists_across_store_instances(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sessions.db"
            first = SQLiteSessionStore(path)
            self.assertTrue(first.accept_event("om_once"))
            second = SQLiteSessionStore(path)
            self.assertFalse(second.accept_event("om_once"))

    def test_feishu_delivery_job_retries_and_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteSessionStore(Path(temp) / "sessions.db")
            payload = {"kind": "reply", "chat_id": "oc_1", "reply": "hello"}
            self.assertTrue(store.enqueue_feishu_job("om_job", payload))
            self.assertFalse(store.enqueue_feishu_job("om_job", payload))

            first = store.claim_feishu_job("om_job")
            self.assertEqual(first.attempts, 1)
            store.save_feishu_reply("om_job", "hello")
            store.retry_feishu_job("om_job", "temporary", 0)
            pending = store.load_feishu_job("om_job")
            self.assertTrue(pending.reply_ready)
            self.assertEqual(pending.reply_text, "hello")

            second = store.claim_feishu_job("om_job")
            self.assertEqual(second.attempts, 2)
            store.complete_feishu_job("om_job")
            self.assertEqual(store.load_feishu_job("om_job").status, "completed")

            store.enqueue_feishu_job("om_interrupted", payload)
            store.claim_feishu_job("om_interrupted")
            self.assertEqual(
                store.recover_feishu_jobs(),
                ["om_interrupted"],
            )
            self.assertEqual(
                store.feishu_job_counts(),
                {"pending": 1, "processing": 0, "completed": 1, "dead": 0},
            )

            interrupted = store.claim_feishu_job("om_interrupted")
            store.fail_feishu_job("om_interrupted", "permanent")
            self.assertEqual(interrupted.attempts, 2)
            self.assertTrue(store.requeue_dead_feishu_job("om_interrupted"))
            self.assertEqual(store.load_feishu_job("om_interrupted").status, "pending")
