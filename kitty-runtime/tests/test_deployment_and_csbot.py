import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, RecordMeta
from kitty.deployment import DeploymentConfigError, DeploymentSettings
from kitty.integrations.csbot import CSBotTurnHandler
from kitty.runtime import KittyRuntime


class FakeSessions:
    def __init__(self):
        self.values = {}

    def get_or_create(self, session_id):
        return self.values.setdefault(session_id, {"history": [], "state": {}})

    def set(self, session_id, value):
        self.values[session_id] = value


class FakeCSAgent:
    def __init__(self):
        self.sessions = FakeSessions()
        self.seen_history = []

    async def handle_message(self, user_id, session_id, message, mentioned=True):
        session = self.sessions.get_or_create(session_id)
        self.seen_history = list(session["history"])
        session["history"].extend(
            [{"role": "user", "content": message}, {"role": "assistant", "content": "real reply"}]
        )
        return "real reply"


class DeploymentSettingsTests(unittest.TestCase):
    def test_development_defaults_to_mock_generic_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = DeploymentSettings.from_env()
        self.assertEqual(settings.environment, "development")
        self.assertEqual(settings.agent_mode, "generic")
        self.assertEqual(settings.model_provider, "mock")
        settings.validate()

    def test_production_requires_all_security_and_business_settings(self):
        settings = DeploymentSettings(
            environment="production",
            agent_mode="csbot",
            project_root=Path(__file__).resolve().parents[2],
            model_provider="mock",
        )
        with self.assertRaises(DeploymentConfigError) as caught:
            settings.validate()
        message = str(caught.exception)
        self.assertIn("FEISHU_ENCRYPT_KEY", message)
        self.assertIn("BITABLE_APP_TOKEN", message)
        self.assertIn("mock model provider is forbidden", message)


class CSBotHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_csbot_bootstrap_loads_tools_and_knowledge(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            environment = {
                "LLM_API_KEY": "test-key",
                "LLM_MODEL": "test-model",
                "LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "FEISHU_APP_ID": "test",
                "FEISHU_APP_SECRET": "test",
                "BITABLE_APP_TOKEN": "test",
                "BITABLE_TABLE_ID": "test",
                "CS_DB_PATH": str(Path(temp) / "cs_agent.db"),
            }
            with patch.dict(os.environ, environment, clear=False):
                handler = CSBotTurnHandler(repo_root)
                try:
                    await handler.startup()
                    self.assertGreater(handler.document_count, 100)
                finally:
                    await handler.shutdown()

    async def test_restores_durable_history_into_csbot_agent(self):
        fake = FakeCSAgent()

        async def factory():
            return fake

        handler = CSBotTurnHandler(Path.cwd(), agent_factory=factory, stream_chunk_size=4)
        emitted = []

        async def emit(kind, payload):
            emitted.append((kind.value, payload))

        result = await handler.run(
            "new message",
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            emit,
            session_id="session",
            record=AgentRecord(user_id="ou_test", meta=RecordMeta(title="test")),
        )
        self.assertEqual(result.reply, "real reply")
        self.assertEqual(len(fake.seen_history), 2)
        self.assertEqual("".join(p["text"] for k, p in emitted if k == "TextPart"), "real reply")

    async def test_runtime_calls_handler_startup(self):
        fake = FakeCSAgent()

        async def factory():
            return fake

        with tempfile.TemporaryDirectory() as temp:
            handler = CSBotTurnHandler(Path.cwd(), agent_factory=factory)
            runtime = KittyRuntime(
                config=KittyConfig(state_dir=Path(temp) / "state"),
                turn_handler=handler,
            )
            try:
                await runtime.start()
                self.assertTrue(runtime.started)
            finally:
                await runtime.close()
