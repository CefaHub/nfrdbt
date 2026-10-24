import tempfile
import unittest
from pathlib import Path

from radar import Gift, RadarState


class TestRadar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "state.json"
        self.state = RadarState(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_poll_baselines_without_alerts(self):
        self.assertEqual(self.state.update_gifts([Gift("1", "Rose", 25)]), [])
        self.assertEqual(self.state.data["pending"], [])
        self.assertEqual(len(self.state.data["gifts"]), 1)

    def test_unlock_creates_one_alert_and_does_not_repeat(self):
        self.state.update_gifts([Gift("1", "Rose", None)])
        events = self.state.update_gifts([Gift("1", "Rose", 25)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "upgrade_unlocked")
        self.assertEqual(self.state.update_gifts([Gift("1", "Rose", 25)]), [])
        self.assertEqual(len(self.state.data["pending"]), 1)

    def test_new_already_upgradeable_gift(self):
        self.state.update_gifts([Gift("1", "Rose", None)])
        events = self.state.update_gifts([Gift("1", "Rose", None), Gift("2", "Cake", 50)])
        self.assertEqual(events[0]["kind"], "new_upgradable_gift")

    def test_pause_suppresses_events_and_updates_snapshot(self):
        self.state.update_gifts([Gift("1", "Rose", None)])
        self.state.set_paused(True)
        self.assertEqual(self.state.update_gifts([Gift("1", "Rose", 25)]), [])
        self.state.set_paused(False)
        self.assertEqual(self.state.update_gifts([Gift("1", "Rose", 25)]), [])

    def test_pause_keeps_nft_alerts_in_outbox(self):
        self.state.set_paused(True)
        self.state.record_nft_once({
            "kind": "new_nft", "slug": "SnakeBox-5",
            "original_participants": [{"role": "recipient", "type": "user",
                                       "id": 101, "label": "Tester"}],
        })
        self.assertEqual(len(self.state.data["pending"]), 1)
        self.state.set_paused(False)
        self.assertEqual(len(RadarState(self.path).data["pending"]), 1)

    def test_price_change_optional(self):
        self.state.update_gifts([Gift("1", "Rose", 25)])
        self.assertEqual(self.state.update_gifts([Gift("1", "Rose", 30)]), [])
        events = self.state.update_gifts([Gift("1", "Rose", 40)], notify_price_changes=True)
        self.assertEqual(events[0]["kind"], "upgrade_price_changed")

    def test_persist_owner_and_delivery_queue(self):
        self.state.update_gifts([Gift("1", "Rose", None)])
        event = self.state.update_gifts([Gift("1", "Rose", 25)])[0]
        self.state.set_owner(42)
        restored = RadarState(self.path)
        self.assertEqual(restored.owner_id, 42)
        self.assertEqual(len(restored.data["pending"]), 1)
        restored.pop_delivered(event["event_id"])
        self.assertFalse(RadarState(self.path).data["pending"])

    def test_omitted_gift_retains_previous_state(self):
        self.state.update_gifts([Gift("1", "Rose", None)])
        self.state.update_gifts([])
        events = self.state.update_gifts([Gift("1", "Rose", 30)])
        self.assertEqual(events[0]["kind"], "upgrade_unlocked")


if __name__ == "__main__":
    unittest.main()
