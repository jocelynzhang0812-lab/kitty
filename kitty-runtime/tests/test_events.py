import unittest

from kitty.core.events import EventType, SessionEvent, WireType


class SessionEventTests(unittest.TestCase):
    def test_wire_contract_matches_observed_hook_shape(self):
        event = SessionEvent.wire(
            "oc_demo",
            WireType.TURN_BEGIN,
            user_input="hello",
            user_id="ou_demo",
        )

        self.assertEqual(event.event_type, "cli.wire")
        self.assertEqual(event.session_id, "oc_demo")
        self.assertEqual(event.data["wire"]["wire_type"], "TurnBegin")
        self.assertEqual(event.data["wire"]["user_input"], "hello")

    def test_event_round_trip(self):
        original = SessionEvent.create(EventType.CLI_TURN_DONE, "session-1", {"ok": True})
        restored = SessionEvent.from_dict(original.to_dict())
        self.assertEqual(restored, original)
