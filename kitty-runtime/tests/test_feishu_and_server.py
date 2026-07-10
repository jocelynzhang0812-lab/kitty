import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kitty.agent.providers.mock import MockProvider
from kitty.channels.feishu import FeishuChallenge, FeishuEventParser, FeishuSender
from kitty.core.config import KittyConfig
from kitty.runtime import KittyRuntime
from kitty.server import KittyASGIApp


def feishu_payload(message_id="om_1", message_type="text", mentions=True):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc_1",
                "message_type": message_type,
                "content": json.dumps({"text": "@CS Bot hello"}),
                "mentions": [{"name": "CS Bot"}] if mentions else [],
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


class FeishuParserTests(unittest.TestCase):
    def test_challenge_and_message(self):
        parser = FeishuEventParser()
        challenge = parser.parse({"challenge": "abc"})
        message = parser.parse(feishu_payload())

        self.assertIsInstance(challenge, FeishuChallenge)
        self.assertEqual(challenge.challenge, "abc")
        self.assertEqual(message.session_id, "oc_1")
        self.assertEqual(message.content, "hello")
        self.assertEqual(message.record.user_id, "ou_1")

    def test_dedupes_and_skips_non_text_or_unmentioned(self):
        parser = FeishuEventParser()
        first = parser.parse(feishu_payload("same"))
        duplicate = parser.parse(feishu_payload("same"))
        non_text = parser.parse(feishu_payload("image", message_type="image"))
        unmentioned = parser.parse(feishu_payload("plain", mentions=False))

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertIsNone(non_text)
        self.assertIsNone(unmentioned)

    def test_verification_token(self):
        parser = FeishuEventParser(verification_token="expected")
        payload = feishu_payload("verified")
        payload["header"]["token"] = "wrong"
        with self.assertRaisesRegex(ValueError, "verification token"):
            parser.parse(payload)


class FeishuSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gets_cached_token_and_sends_text(self):
        calls = []

        def transport(url, headers, payload):
            calls.append((url, headers, payload))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "token", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_reply"}}

        sender = FeishuSender(app_id="app", app_secret="secret", transport=transport)
        first = await sender.send_text("oc_1", "hello")
        second = await sender.send_text("oc_1", "again")

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][1]["Authorization"], "Bearer token")


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = KittyConfig(state_dir=Path(self.temp.name) / "state")
        self.runtime = KittyRuntime(config=config, provider=MockProvider(prefix="api:"))
        self.app = KittyASGIApp(self.runtime)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_health_and_debug_message(self):
        health_status, health = await asgi_request(self.app, "GET", "/health")
        status, body = await asgi_request(
            self.app,
            "POST",
            "/v1/messages",
            {"session_id": "api-session", "message": "hello"},
        )

        self.assertEqual(health_status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "api:hello")

    async def test_feishu_challenge_and_message(self):
        status, challenge = await asgi_request(
            self.app,
            "POST",
            "/feishu/events",
            {"challenge": "verify"},
        )
        message_status, message = await asgi_request(
            self.app,
            "POST",
            "/feishu/events",
            feishu_payload("server-message"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(challenge, {"challenge": "verify"})
        self.assertEqual(message_status, 200)
        self.assertEqual(message["reply"], "api:hello")

    async def test_feishu_with_sender_acknowledges_before_background_reply(self):
        replies = []

        async def sender(chat_id, text):
            replies.append((chat_id, text))

        app = KittyASGIApp(
            self.runtime,
            feishu_parser=FeishuEventParser(),
            reply_sender=sender,
        )
        status, body = await asgi_request(
            app,
            "POST",
            "/feishu/events",
            feishu_payload("background-message"),
        )
        await asyncio.gather(*tuple(app._background_tasks))

        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(replies, [("oc_1", "api:hello")])
