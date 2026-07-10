import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from Crypto.Cipher import AES

from kitty.agent.providers.mock import MockProvider
from kitty.channels.feishu import (
    FeishuChallenge,
    FeishuEventParser,
    FeishuSender,
    UnsupportedFeishuMessage,
)
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
                "content": json.dumps({"text": "@Kitty Bot hello"}),
                "mentions": [{"name": "Kitty Bot"}] if mentions else [],
            },
        },
    }


async def asgi_request(app, method, path, payload=None, headers=None):
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
        self.assertIsInstance(non_text, UnsupportedFeishuMessage)
        self.assertIsNone(unmentioned)

    def test_verification_token(self):
        parser = FeishuEventParser(verification_token="expected")
        payload = feishu_payload("verified")
        payload["header"]["token"] = "wrong"
        with self.assertRaisesRegex(ValueError, "verification token"):
            parser.parse(payload)

    def test_p2p_message_does_not_require_mention(self):
        parser = FeishuEventParser(require_mention=True)
        payload = feishu_payload("p2p", mentions=False)
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps({"text": "hello"})
        message = parser.parse(payload)
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "hello")

    def test_signature_and_aes_decryption(self):
        encrypt_key = "encrypt-key-for-test"
        verification_token = "verify-token"
        plaintext = json.dumps(
            {"challenge": "encrypted-challenge", "token": verification_token},
            ensure_ascii=False,
        ).encode("utf-8")
        padding = AES.block_size - len(plaintext) % AES.block_size
        padded = plaintext + bytes([padding]) * padding
        iv = b"0123456789abcdef"
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        encrypted = base64.b64encode(iv + AES.new(key, AES.MODE_CBC, iv).encrypt(padded)).decode()
        body = json.dumps({"encrypt": encrypted}).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = "nonce"
        signature = hashlib.sha256(
            (timestamp + nonce + encrypt_key).encode("utf-8") + body
        ).hexdigest()
        parser = FeishuEventParser(
            encrypt_key=encrypt_key,
            verification_token=verification_token,
        )
        parsed = parser.parse_http(
            body,
            {
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": signature,
            },
        )
        self.assertIsInstance(parsed, FeishuChallenge)
        self.assertEqual(parsed.challenge, "encrypted-challenge")

        with self.assertRaisesRegex(ValueError, "signature"):
            parser.parse_http(
                body,
                {
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": "wrong",
                },
            )


class FeishuSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gets_cached_token_and_sends_text(self):
        calls = []

        def transport(url, headers, payload, method="POST"):
            calls.append((url, headers, payload))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "token", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_reply"}}

        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=transport,
            min_send_interval_seconds=0,
        )
        first = await sender.send_text("oc_1", "hello", "delivery-uuid")
        second = await sender.send_text("oc_1", "again")

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][1]["Authorization"], "Bearer token")
        self.assertEqual(calls[1][2]["uuid"], "delivery-uuid")


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

    async def test_debug_api_can_be_disabled_or_token_protected(self):
        disabled = KittyASGIApp(self.runtime, debug_api_enabled=False)
        disabled_status, _ = await asgi_request(
            disabled,
            "POST",
            "/v1/messages",
            {"session_id": "debug", "message": "hello"},
        )
        protected = KittyASGIApp(self.runtime, debug_api_token="secret")
        rejected_status, _ = await asgi_request(
            protected,
            "POST",
            "/v1/messages",
            {"session_id": "debug", "message": "hello"},
        )
        accepted_status, accepted = await asgi_request(
            protected,
            "POST",
            "/v1/messages",
            {"session_id": "debug", "message": "hello"},
            {"Authorization": "Bearer secret"},
        )

        self.assertEqual(disabled_status, 404)
        self.assertEqual(rejected_status, 401)
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted["reply"], "api:hello")

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

    async def test_durable_delivery_retries_without_rerunning_model(self):
        calls = []

        async def fallback_sender(chat_id, text):
            raise AssertionError("idempotent sender should be used")

        async def idempotent_sender(chat_id, text, request_uuid):
            calls.append((chat_id, text, request_uuid))
            if len(calls) == 1:
                raise RuntimeError("temporary Feishu failure")

        app = KittyASGIApp(
            self.runtime,
            feishu_parser=FeishuEventParser(),
            reply_sender=fallback_sender,
            idempotent_reply_sender=idempotent_sender,
            delivery_retry_base_seconds=0.01,
        )
        status, body = await asgi_request(
            app,
            "POST",
            "/feishu/events",
            feishu_payload("durable-message"),
        )
        await asyncio.gather(*tuple(app._background_tasks))

        job = self.runtime.store.load_feishu_job("durable-message")
        session = self.runtime.store.load("oc_1")
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(len(session.messages), 2)
