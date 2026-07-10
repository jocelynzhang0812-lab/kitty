import json
import tempfile
import unittest
from pathlib import Path

from kitty.agent.providers.mock import MockProvider
from kitty.channels.base import ChannelMessage
from kitty.channels.feishu import FeishuEventParser, FeishuSender
from kitty.channels.feishu_cards import button_card, select_card, text_card
from kitty.core.config import KittyConfig
from kitty.runtime import KittyRuntime
from kitty.server import KittyASGIApp

from tests.test_feishu_and_server import asgi_request


def card_action_payload(event_id="evt-card-1", value=None, option=None, tag="button"):
    action = {"tag": tag}
    if value is not None:
        action["value"] = value
    if option is not None:
        action["option"] = option
    return {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "event_id": event_id},
        "event": {
            "operator": {"open_id": "ou_clicker"},
            "action": action,
            "context": {"open_message_id": "om_card", "open_chat_id": "oc_1"},
        },
    }


class CardBuilderTests(unittest.TestCase):
    def test_text_card_with_title_and_note(self):
        card = text_card("正文", title="标题", template="green", note="✅ 已处理")

        self.assertEqual(card["header"]["title"]["content"], "标题")
        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(card["elements"][0]["text"]["tag"], "lark_md")
        self.assertEqual(card["elements"][0]["text"]["content"], "正文")
        self.assertEqual(card["elements"][1]["tag"], "note")
        self.assertEqual(card["elements"][1]["elements"][0]["content"], "✅ 已处理")

    def test_text_card_minimal_plaintext(self):
        card = text_card("hi", markdown=False)

        self.assertNotIn("header", card)
        self.assertEqual(card["elements"][0]["text"]["tag"], "plain_text")
        self.assertEqual(len(card["elements"]), 1)

    def test_button_card_actions(self):
        card = button_card(
            "工单 #7",
            [
                {"text": "解决", "value": {"action": "resolve", "id": "7"}, "type": "primary"},
                {"text": "文档", "url": "https://example.com"},
            ],
        )
        actions = card["elements"][1]["actions"]

        self.assertEqual(card["elements"][1]["tag"], "action")
        self.assertEqual(actions[0]["text"]["content"], "解决")
        self.assertEqual(actions[0]["type"], "primary")
        self.assertEqual(actions[0]["value"], {"action": "resolve", "id": "7"})
        self.assertEqual(actions[1]["url"], "https://example.com")
        self.assertNotIn("value", actions[1])

    def test_select_card_options_and_value(self):
        card = select_card(
            "请选择产品线",
            placeholder="选一个",
            options=[{"text": "云端", "value": "cloud"}, {"text": "桌面", "value": "desktop"}],
            value={"action": "pick_product"},
        )
        select = card["elements"][1]["actions"][0]

        self.assertEqual(select["tag"], "select_static")
        self.assertEqual(select["placeholder"]["content"], "选一个")
        self.assertEqual(select["options"][1]["value"], "desktop")
        self.assertEqual(select["value"], {"action": "pick_product"})


class CardActionParserTests(unittest.TestCase):
    def test_button_click_becomes_channel_message(self):
        parser = FeishuEventParser()
        parsed = parser.parse(
            card_action_payload(value={"action": "resolve", "text": "标记为已解决"})
        )

        self.assertIsInstance(parsed, ChannelMessage)
        self.assertEqual(parsed.session_id, "oc_1")
        self.assertEqual(parsed.content, "标记为已解决")
        self.assertEqual(parsed.request_id, "evt-card-1")
        self.assertEqual(parsed.record.user_id, "ou_clicker")
        extra = parsed.record.meta.extra
        self.assertEqual(extra["kind"], "card_action")
        self.assertEqual(extra["card_value"], {"action": "resolve", "text": "标记为已解决"})
        self.assertEqual(extra["message_id"], "om_card")

    def test_select_option_content(self):
        parser = FeishuEventParser()
        parsed = parser.parse(
            card_action_payload(value={"action": "pick"}, option="desktop", tag="select_static")
        )

        self.assertEqual(parsed.content, "[选择] desktop")
        self.assertEqual(parsed.record.meta.extra["card_option"], "desktop")

    def test_value_without_text_falls_back_to_json(self):
        parser = FeishuEventParser()
        parsed = parser.parse(card_action_payload(value={"action": "escalate"}))

        self.assertTrue(parsed.content.startswith("[卡片操作] "))
        self.assertIn("escalate", parsed.content)

    def test_card_action_deduped_by_event_id(self):
        parser = FeishuEventParser()
        first = parser.parse(card_action_payload(event_id="evt-same", value={"text": "x"}))
        duplicate = parser.parse(card_action_payload(event_id="evt-same", value={"text": "x"}))

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)

    def test_card_action_respects_verification_token(self):
        parser = FeishuEventParser(verification_token="expected")
        payload = card_action_payload(value={"text": "x"})
        payload["header"]["token"] = "wrong"

        with self.assertRaisesRegex(ValueError, "verification token"):
            parser.parse(payload)

    def test_card_action_ignores_mention_requirement(self):
        parser = FeishuEventParser(require_mention=True)
        parsed = parser.parse(card_action_payload(value={"text": "点了按钮"}))

        self.assertIsNotNone(parsed)


class InteractiveSenderTests(unittest.IsolatedAsyncioTestCase):
    def _sender(self, calls):
        def transport(url, headers, payload, method="POST"):
            calls.append((url, method, payload))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "token", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_new"}}

        return FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=transport,
            min_send_interval_seconds=0,
        )

    async def test_send_card_posts_interactive(self):
        calls = []
        sender = self._sender(calls)
        card = text_card("hello card")
        await sender.send_card("oc_1", card, "uuid-1")

        url, method, payload = calls[-1]
        self.assertIn("/im/v1/messages", url)
        self.assertEqual(method, "POST")
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["uuid"], "uuid-1")
        self.assertEqual(json.loads(payload["content"]), card)

    async def test_update_card_patches_message(self):
        calls = []
        sender = self._sender(calls)
        card = text_card("已解决", note="✅ 由 ou_x 处理")
        await sender.update_card("om_card", card)

        url, method, payload = calls[-1]
        self.assertTrue(url.endswith("/im/v1/messages/om_card"))
        self.assertEqual(method, "PATCH")
        self.assertEqual(json.loads(payload["content"]), card)

    async def test_add_reaction(self):
        calls = []
        sender = self._sender(calls)
        await sender.add_reaction("om_1", "OnIt")

        url, method, payload = calls[-1]
        self.assertTrue(url.endswith("/im/v1/messages/om_1/reactions"))
        self.assertEqual(method, "POST")
        self.assertEqual(payload["reaction_type"], {"emoji_type": "OnIt"})

    async def test_api_error_clears_token_and_raises(self):
        calls = []

        def transport(url, headers, payload, method="POST"):
            calls.append(url)
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "token", "expire": 7200}
            return {"code": 99991663, "msg": "token invalid"}

        sender = FeishuSender(
            app_id="app",
            app_secret="secret",
            transport=transport,
            min_send_interval_seconds=0,
        )
        with self.assertRaisesRegex(RuntimeError, "add reaction failed"):
            await sender.add_reaction("om_1", "DONE")
        # Token cache must be cleared so the durable retry refetches it.
        self.assertEqual(sender._token, "")


class CardActionServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = KittyConfig(state_dir=Path(self.temp.name) / "state")
        self.runtime = KittyRuntime(config=config, provider=MockProvider(prefix="api:"))
        self.app = KittyASGIApp(self.runtime)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_card_action_dispatches_through_runtime(self):
        status, body = await asgi_request(
            self.app,
            "POST",
            "/feishu/events",
            card_action_payload(value={"action": "resolve", "text": "标记为已解决"}),
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "api:标记为已解决")
        self.assertEqual(body["session_id"], "oc_1")


if __name__ == "__main__":
    unittest.main()
