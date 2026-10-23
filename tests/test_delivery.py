import asyncio
import tempfile
import unittest
from pathlib import Path

from delivery import dispatch_next, fallback_text, is_bad_markup
from radar import Gift, RadarState


class InvalidButtonError(Exception):
    pass


class ButtonUrlInvalidError(Exception):
    pass


class MessageEntityInvalidError(Exception):
    pass


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "state.json"
        self.state = RadarState(self.file)
        self.state.set_owner(321)

    def tearDown(self):
        self.tmp.cleanup()

    def add_nft(self, index):
        self.state.record_rare_nft({
            "kind": "new_nft", "slug": f"PlushPepe-{index}",
            "title": "Plush Pepe", "number": index,
            "model": "Common", "model_rarity": "15%",
        })

    async def test_sent_only_after_network_confirmed(self):
        self.add_nft(123)
        calls = []

        async def rich(item):
            calls.append(("rich", item["slug"]))

        async def plain(item):
            self.fail("Fallback should not be used")

        result, item = await dispatch_next(self.state, rich, plain)
        self.assertEqual(result, "sent")
        self.assertEqual(item["slug"], "PlushPepe-123")
        self.assertEqual(calls, [("rich", "PlushPepe-123")])
        self.assertFalse(self.state.data["pending"])
        self.assertFalse(RadarState(self.file).data["pending"])

    async def test_failed_send_remains_persisted_after_restart(self):
        self.add_nft(123)

        async def fail(item):
            raise ConnectionError("Telegram network timeout")

        async def plain(item):
            self.fail("No fallback for transient network failure")

        with self.assertRaises(ConnectionError):
            await dispatch_next(self.state, fail, plain)
        restored = RadarState(self.file)
        self.assertEqual(restored.data["pending"][0]["slug"], "PlushPepe-123")

        async def success(item):
            return None

        await dispatch_next(restored, success, plain)
        self.assertEqual(RadarState(self.file).data["pending"], [])

    async def test_bad_inline_markup_falls_back_to_plain(self):
        self.add_nft(404)
        calls = []

        async def rich(item):
            raise InvalidButtonError("BUTTON_URL_INVALID")

        async def plain(item):
            calls.append(fallback_text(item))

        result, _ = await dispatch_next(self.state, rich, plain)
        self.assertEqual(result, "plain")
        self.assertIn("https://t.me/nft/PlushPepe-404", calls[0])
        self.assertEqual(self.state.data["pending"], [])

    async def test_fallback_failure_does_not_lose_alert(self):
        self.add_nft(200)

        async def rich(item):
            raise InvalidButtonError("BUTTON_URL_INVALID")

        async def plain(item):
            raise ConnectionError("Network still down")

        with self.assertRaises(ConnectionError):
            await dispatch_next(self.state, rich, plain)
        self.assertEqual(len(self.state.data["pending"]), 1)

    async def test_paused_and_unpaired_do_not_send(self):
        self.add_nft(123)
        calls = []

        async def sender(item):
            calls.append(item)

        self.state.set_paused(True)
        result, _ = await dispatch_next(self.state, sender, sender)
        self.assertEqual(result, "idle")
        self.state.set_paused(False)
        self.state.data["owner_id"] = None
        result, _ = await dispatch_next(self.state, sender, sender)
        self.assertEqual(result, "idle")
        self.assertFalse(calls)
        self.assertEqual(len(self.state.data["pending"]), 1)

    async def test_no_100_event_truncation_and_order(self):
        for number in range(101, 246):
            self.add_nft(number)
        self.assertEqual(len(self.state.data["pending"]), 145)
        self.assertEqual(len(RadarState(self.file).data["pending"]), 145)
        sent = []

        async def sender(item):
            sent.append(item["slug"])

        for _ in range(3):
            await dispatch_next(self.state, sender, sender)
        self.assertEqual(sent, ["PlushPepe-101", "PlushPepe-102", "PlushPepe-103"])
        self.assertEqual(len(self.state.data["pending"]), 142)

    async def test_oldest_event_sent_first_after_retry(self):
        self.add_nft(1)
        self.add_nft(2)
        sent = []

        async def sender(item):
            sent.append(item["slug"])

        await dispatch_next(self.state, sender, sender)
        await dispatch_next(self.state, sender, sender)
        self.assertEqual(sent, ["PlushPepe-1", "PlushPepe-2"])

    async def test_telethon_camelcase_button_error_falls_back(self):
        self.add_nft(333)
        delivered = []

        async def rich(item):
            raise ButtonUrlInvalidError("invalid URL")

        async def plain(item):
            delivered.append(fallback_text(item))

        result, item = await dispatch_next(self.state, rich, plain)
        self.assertEqual(result, "plain")
        self.assertEqual(item["slug"], "PlushPepe-333")
        self.assertEqual(len(delivered), 1)
        self.assertFalse(self.state.data["pending"])

    def test_telethon_camelcase_entity_error_is_detected(self):
        self.assertTrue(is_bad_markup(MessageEntityInvalidError("bad entities")))

    def test_transient_errors_are_not_treated_as_markup(self):
        self.assertFalse(is_bad_markup(ConnectionError("Connection closed")))
        self.assertFalse(is_bad_markup(asyncio.TimeoutError()))
        self.assertTrue(is_bad_markup(InvalidButtonError("BUTTON_URL_INVALID")))


if __name__ == "__main__":
    unittest.main()
