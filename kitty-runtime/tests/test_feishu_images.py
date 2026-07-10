import json
import unittest

from kitty.channels.base import ChannelMessage
from kitty.channels.feishu import FeishuEventParser, FeishuSender, UnsupportedFeishuMessage


def image_payload(message_id="om_img", image_key="img_key_1", chat_type="p2p", mentions=None):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc_1",
                "chat_type": chat_type,
                "message_type": "image",
                "content": json.dumps({"image_key": image_key}),
                "mentions": mentions or [],
            },
        },
    }


class ImageParserTests(unittest.TestCase):
    def test_default_keeps_unsupported_behavior(self):
        parser = FeishuEventParser()
        parsed = parser.parse(image_payload())

        self.assertIsInstance(parsed, UnsupportedFeishuMessage)
        self.assertEqual(parsed.message_type, "image")

    def test_accept_images_yields_channel_message_with_image_key(self):
        parser = FeishuEventParser(accept_images=True)
        parsed = parser.parse(image_payload())

        self.assertIsInstance(parsed, ChannelMessage)
        self.assertEqual(parsed.content, "[用户发送了一张图片]")
        self.assertEqual(parsed.session_id, "oc_1")
        self.assertEqual(parsed.request_id, "om_img")
        extra = parsed.record.meta.extra
        self.assertEqual(extra["kind"], "image")
        self.assertEqual(extra["image_key"], "img_key_1")
        self.assertEqual(extra["message_id"], "om_img")

    def test_accept_images_missing_key_is_dropped(self):
        parser = FeishuEventParser(accept_images=True)
        parsed = parser.parse(image_payload(image_key=""))

        self.assertIsNone(parsed)

    def test_group_image_still_requires_mention(self):
        parser = FeishuEventParser(accept_images=True, require_mention=True)
        parsed = parser.parse(image_payload(chat_type="group"))

        self.assertIsNone(parsed)

    def test_group_image_with_mention_accepted(self):
        parser = FeishuEventParser(accept_images=True, require_mention=True)
        parsed = parser.parse(
            image_payload(chat_type="group", mentions=[{"name": "Kitty Bot"}])
        )

        self.assertIsInstance(parsed, ChannelMessage)


class ImageSenderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _json_transport(calls):
        def transport(url, headers, payload, method="POST"):
            calls.append((url, method, payload))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "token", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_new"}}

        return transport

    async def test_download_resource_uses_bearer_token(self):
        json_calls = []
        binary_calls = []

        def binary_transport(url, headers):
            binary_calls.append((url, headers))
            return b"image-bytes"

        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=self._json_transport(json_calls),
            binary_transport=binary_transport,
            min_send_interval_seconds=0,
        )
        data = await sender.download_resource("om_img", "img_key_1")

        self.assertEqual(data, b"image-bytes")
        url, headers = binary_calls[0]
        self.assertIn("/im/v1/messages/om_img/resources/img_key_1", url)
        self.assertIn("type=image", url)
        self.assertEqual(headers["Authorization"], "Bearer token")

    async def test_upload_image_returns_key(self):
        json_calls = []
        upload_calls = []

        def upload_transport(url, headers, fields, file_field, file_name, file_bytes):
            upload_calls.append((url, fields, file_field, file_bytes))
            return {"code": 0, "data": {"image_key": "img_key_new"}}

        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=self._json_transport(json_calls),
            upload_transport=upload_transport,
            min_send_interval_seconds=0,
        )
        key = await sender.upload_image(b"raw-bytes")

        self.assertEqual(key, "img_key_new")
        url, fields, file_field, file_bytes = upload_calls[0]
        self.assertTrue(url.endswith("/im/v1/images"))
        self.assertEqual(fields, {"image_type": "message"})
        self.assertEqual(file_field, "image")
        self.assertEqual(file_bytes, b"raw-bytes")

    async def test_upload_failure_clears_token(self):
        json_calls = []

        def upload_transport(url, headers, fields, file_field, file_name, file_bytes):
            return {"code": 234001, "msg": "bad image"}

        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=self._json_transport(json_calls),
            upload_transport=upload_transport,
            min_send_interval_seconds=0,
        )
        with self.assertRaisesRegex(RuntimeError, "upload image failed"):
            await sender.upload_image(b"raw")
        self.assertEqual(sender._token, "")

    async def test_send_image_posts_image_key(self):
        json_calls = []
        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=self._json_transport(json_calls),
            min_send_interval_seconds=0,
        )
        await sender.send_image("oc_1", "img_key_1", "uuid-img")

        url, method, payload = json_calls[-1]
        self.assertIn("/im/v1/messages", url)
        self.assertEqual(payload["msg_type"], "image")
        self.assertEqual(json.loads(payload["content"]), {"image_key": "img_key_1"})
        self.assertEqual(payload["uuid"], "uuid-img")


if __name__ == "__main__":
    unittest.main()
