import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kitty.cli import build_parser, run_cli
from kitty.config_file import EnvFileError, load_env_file, write_env_file
from kitty.onboarding import OnboardingASGIApp, OnboardingService


def valid_payload():
    return {
        "bot_name": "Team Bot",
        "purpose": "Help the team",
        "system_prompt": "You are Team Bot. Be concise.",
        "public_base_url": "https://bot.example.com",
        "model_api_key": "model-secret",
        "model_base_url": "https://model.example.com/v1",
        "model_name": "test-model",
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "feishu-secret",
        "feishu_verification_token": "verify-secret",
        "feishu_encrypt_key": "encrypt-secret",
        "require_mention": True,
        "accept_images": False,
        "tool_modules": "examples.tools",
        "hook_paths": "",
    }


def fake_poster(url, headers, payload, timeout):
    if "tenant_access_token" in url:
        return {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
    return {"choices": [{"message": {"content": "OK"}}]}


async def asgi_request(app, method, path, payload=None, headers=None, query_string=b""):
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
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": [
                (str(key).encode("latin-1"), str(value).encode("latin-1"))
                for key, value in (headers or {}).items()
            ],
        },
        receive,
        send,
    )
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = next(item["body"] for item in sent if item["type"] == "http.response.body")
    content_type = next(item for item in sent if item["type"] == "http.response.start")[
        "headers"
    ][0][1]
    return status, json.loads(body) if b"json" in content_type else body.decode("utf-8")


class EnvFileTests(unittest.TestCase):
    def test_writes_private_file_and_loads_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            write_env_file(path, {"NAME": "Kitty Bot", "TOKEN": "a=b#c"})
            mode = path.stat().st_mode & 0o777
            with patch.dict(os.environ, {}, clear=True):
                values = load_env_file(path)
                self.assertEqual(os.environ["TOKEN"], "a=b#c")
        self.assertEqual(mode, 0o600)
        self.assertEqual(values["NAME"], "Kitty Bot")

    def test_rejects_multiline_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(EnvFileError):
                write_env_file(Path(temp) / ".env", {"TOKEN": "one\ntwo"})

    def test_rejects_invalid_environment_key(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(EnvFileError):
                write_env_file(Path(temp) / ".env", {"BAD-KEY": "secret"})


class OnboardingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_check_and_save(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parents[1]
            output = Path(temp) / ".env"
            service = OnboardingService(
                project_root=root,
                output_path=output,
                json_poster=fake_poster,
            )
            result = await service.check(valid_payload(), live=True)
            saved = service.save(valid_payload())
            content = output.read_text(encoding="utf-8")

        self.assertTrue(result["ready"])
        self.assertEqual(len(result["checks"]), 5)
        self.assertEqual(saved["callback_url"], "https://bot.example.com/feishu/events")
        self.assertNotIn("model-secret", json.dumps(saved))
        self.assertIn("LLM_API_KEY=model-secret", content)

    async def test_rejects_non_https_public_url(self):
        payload = valid_payload()
        payload["public_base_url"] = "http://localhost:8000"
        with tempfile.TemporaryDirectory() as temp:
            service = OnboardingService(
                project_root=Path(__file__).resolve().parents[1],
                output_path=Path(temp) / ".env",
                json_poster=fake_poster,
            )
            errors = service.validate(payload)
        self.assertTrue(any("https://" in error for error in errors))

    async def test_missing_fields_use_concise_user_facing_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            service = OnboardingService(
                project_root=Path(__file__).resolve().parents[1],
                output_path=Path(temp) / ".env",
            )
            errors = service.validate({})

        self.assertIn("缺少模型 API Key", errors)
        self.assertFalse(any("missing production setting" in error for error in errors))


class OnboardingAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_token_and_only_saves_verified_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / ".env"
            service = OnboardingService(
                project_root=Path(__file__).resolve().parents[1],
                output_path=output,
                json_poster=fake_poster,
            )
            app = OnboardingASGIApp(service, setup_token="setup-secret")
            forbidden, _ = await asgi_request(app, "GET", "/")
            page_status, page = await asgi_request(
                app,
                "GET",
                "/",
                query_string=b"token=setup-secret",
            )
            headers = {"X-Kitty-Setup-Token": "setup-secret"}
            check_status, checked = await asgi_request(
                app, "POST", "/api/check", valid_payload(), headers
            )
            changed = valid_payload()
            changed["bot_name"] = "Changed"
            rejected_status, _ = await asgi_request(
                app, "POST", "/api/save", changed, headers
            )
            save_status, saved = await asgi_request(
                app, "POST", "/api/save", valid_payload(), headers
            )

        self.assertEqual(forbidden, 403)
        self.assertEqual(page_status, 200)
        self.assertIn("10 分钟", page)
        self.assertEqual(check_status, 200)
        self.assertTrue(checked["ready"])
        self.assertEqual(rejected_status, 400)
        self.assertEqual(save_status, 200)
        self.assertTrue(saved["saved"])


class CliTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_command_uses_saved_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parents[1]
            path = Path(temp) / ".env"
            service = OnboardingService(project_root=root, output_path=path)
            payload = valid_payload()
            payload["state_dir"] = str(Path(temp) / "production-state")
            write_env_file(path, service.environment_values(payload))
            args = build_parser().parse_args(
                ["doctor", "--env-file", str(path), "--json"]
            )
            with patch("builtins.print") as printer:
                code = await run_cli(args)
        self.assertEqual(code, 0)
        result = json.loads(printer.call_args.args[0])
        storage = next(item for item in result["checks"] if item["key"] == "storage")
        self.assertEqual(storage["detail"], str((Path(temp) / "production-state").resolve()))


if __name__ == "__main__":
    unittest.main()
