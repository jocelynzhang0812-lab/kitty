import os
import unittest
from pathlib import Path
from unittest.mock import patch

from kitty.deployment import DeploymentConfigError, DeploymentSettings, build_runtime


class DeploymentSettingsTests(unittest.TestCase):
    def test_development_defaults_to_mock_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = DeploymentSettings.from_env()
        self.assertEqual(settings.environment, "development")
        self.assertEqual(settings.model_provider, "mock")
        settings.validate()

    def test_production_requires_only_model_and_feishu_settings(self):
        settings = DeploymentSettings(
            environment="production",
            project_root=Path(__file__).resolve().parents[1],
            model_provider="mock",
        )
        with self.assertRaises(DeploymentConfigError) as caught:
            settings.validate()
        message = str(caught.exception)
        self.assertIn("FEISHU_ENCRYPT_KEY", message)
        self.assertIn("LLM_API_KEY", message)
        self.assertIn("mock model provider is forbidden", message)

    def test_distributed_worker_does_not_require_feishu_secrets(self):
        settings = DeploymentSettings(
            environment="production",
            project_root=Path(__file__).resolve().parents[1],
            bot_name="Worker Bot",
            model_provider="openai_compatible",
            model_api_key="model-secret",
            model_name="test-model",
        )
        settings.validate(role="worker")

    def test_loads_generic_tool_and_hook_extensions(self):
        root = Path(__file__).resolve().parents[1]
        settings = DeploymentSettings(
            environment="test",
            project_root=root,
            model_provider="mock",
            tool_modules=("examples.tools",),
            hook_paths=("examples/echo_hook.py",),
        )
        runtime = build_runtime(settings)
        try:
            names = [schema["function"]["name"] for schema in runtime.tools.schemas()]
            self.assertEqual(names, ["calculate_sum"])
            self.assertEqual(len(runtime.loaded_hooks), 1)
        finally:
            import asyncio

            asyncio.run(runtime.close())
