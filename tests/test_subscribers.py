import tempfile
import unittest
from pathlib import Path

from delivery import dispatch_next
from radar import RadarState


class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = RadarState(self.path)
        self.state.set_owner(101)

    def tearDown(self):
        self.temp.cleanup()

    def test_invite_is_one_time_hashed_and_persistent(self):
        token = self.state.create_invite()
        self.assertNotIn(token, self.path.read_text(encoding="utf-8"))
        self.assertTrue(self.state.accept_invite(token, 202))
        self.assertFalse(self.state.accept_invite(token, 303))
        recovered = RadarState(self.path)
        self.assertEqual(recovered.recipient_ids, [101, 202])
        self.assertFalse(recovered.accept_invite(token, 303))
        self.assertEqual(recovered.owner_id, 101)

    def test_other_users_cannot_become_owner(self):
        token = self.state.create_invite()
        self.assertFalse(self.state.accept_invite(token, 101))
        self.assertFalse(self.state.accept_invite("wrong", 202))
        self.assertEqual(self.state.recipient_ids, [101])

    def test_unsubscribe_removes_future_delivery_targets(self):
        token = self.state.create_invite()
        self.assertTrue(self.state.accept_invite(token, 202))
        self.state.record_rare_nft({"kind": "new_nft", "slug": "SnakeBox-1"})
        self.assertEqual(self.state.data["pending"][0]["targets"], [101, 202])
        self.assertTrue(self.state.remove_subscriber(202))
        self.assertEqual(self.state.data["pending"][0]["targets"], [101])
        self.assertEqual(self.state.recipient_ids, [101])


class MultiDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = RadarState(self.path)
        self.state.set_owner(101)
        self.assertTrue(self.state.accept_invite(self.state.create_invite(), 202))

    def tearDown(self):
        self.temp.cleanup()

    def add_nft(self, slug="SnakeBox-1"):
        self.state.record_rare_nft({"kind": "new_nft", "slug": slug})

    async def test_both_recipients_receive_one_copy(self):
        self.add_nft()
        sent = []

        async def send(item):
            sent.append((item["_target_id"], item["slug"]))

        first, _ = await dispatch_next(self.state, send, send)
        self.assertEqual(first, "sent")
        self.assertEqual(len(self.state.data["pending"]), 1)
        self.assertEqual(self.state.data["pending"][0]["delivered_to"], [101])
        restored = RadarState(self.path)
        second, _ = await dispatch_next(restored, send, send)
        self.assertEqual(second, "sent")
        self.assertEqual(sent, [(101, "SnakeBox-1"), (202, "SnakeBox-1")])
        self.assertEqual(restored.data["pending"], [])

    async def test_friend_delivery_retry_does_not_resend_owner(self):
        self.add_nft()
        calls = []

        async def send(item):
            calls.append(item["_target_id"])
            if item["_target_id"] == 202 and calls.count(202) == 1:
                raise ConnectionError("temporary network problem")

        await dispatch_next(self.state, send, send)
        with self.assertRaises(ConnectionError):
            await dispatch_next(self.state, send, send)
        self.assertEqual(self.state.data["pending"][0]["delivered_to"], [101])
        await dispatch_next(self.state, send, send)
        self.assertEqual(calls, [101, 202, 202])
        self.assertEqual(self.state.data["pending"], [])

    async def test_existing_pending_is_not_sent_to_new_friend(self):
        state = RadarState(Path(self.temp.name) / "second-state.json")
        state.set_owner(101)
        state.record_rare_nft({"kind": "new_nft", "slug": "SnakeBox-11"})
        state.accept_invite(state.create_invite(), 202)
        calls = []

        async def send(item):
            calls.append(item["_target_id"])

        await dispatch_next(state, send, send)
        self.assertEqual(calls, [101])
        self.assertEqual(state.data["pending"], [])

    async def test_legacy_queue_still_works(self):
        state = RadarState(Path(self.temp.name) / "legacy-state.json")
        state.set_owner(101)
        state.data["pending"] = [{"kind": "new_nft", "slug": "SnakeBox-10",
                                  "event_id": "legacy-id"}]
        state.save()
        calls = []

        async def send(item):
            calls.append(item["_target_id"])

        await dispatch_next(state, send, send)
        self.assertEqual(calls, [101])
        self.assertEqual(state.data["pending"], [])


if __name__ == "__main__":
    unittest.main()
