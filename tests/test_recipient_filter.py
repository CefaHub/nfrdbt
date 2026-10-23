import tempfile
import unittest
from pathlib import Path

from radar import RadarState
from recipient_filter import eligible_auto_alert, filter_pending, recipient_from_history


RECIPIENT = {
    "role": "recipient", "type": "user", "id": 999,
    "label": "@Recipient999", "url": "https://t.me/Recipient999",
}
SENDER = {
    "role": "sender", "type": "user", "id": 10,
    "label": "@SenderName", "url": "https://t.me/SenderName",
}


class RecipientOnlyTests(unittest.TestCase):
    def test_real_original_recipient_qualifies(self):
        alert = {"kind": "new_nft", "slug": "SpicedWine-108832",
                 "original_participants": [SENDER, RECIPIENT]}
        self.assertTrue(eligible_auto_alert(alert))
        self.assertEqual(recipient_from_history(alert["original_participants"]), RECIPIENT)

    def test_sender_only_is_not_enough(self):
        alert = {"kind": "new_nft", "original_participants": [SENDER]}
        self.assertFalse(eligible_auto_alert(alert))

    def test_only_display_name_not_enough(self):
        alert = {"kind": "rare_nft", "owner_name": "𝒜𝒷𝑒𝓁",
                 "original_participants": []}
        self.assertFalse(eligible_auto_alert(alert))

    def test_recipient_requires_telegram_peer_and_link(self):
        self.assertIsNone(recipient_from_history([
            {"role": "recipient", "type": "name", "id": 1,
             "url": "https://t.me/Unknown"}
        ]))
        self.assertIsNone(recipient_from_history([
            {"role": "recipient", "type": "user", "id": 1, "url": ""}
        ]))
        self.assertIsNotNone(recipient_from_history([
            {"role": "recipient", "type": "user", "id": 123,
             "url": "tg://user?id=123"}
        ]))

    def test_ordinary_market_and_upgrade_alerts_are_silent(self):
        for kind in ("market_deal", "upgrade_unlocked", "new_upgradable_gift",
                     "upgrade_price_changed"):
            self.assertFalse(eligible_auto_alert({
                "kind": kind, "original_participants": [RECIPIENT],
            }))

    def test_explicit_delivery_smoke_test_is_allowed(self):
        self.assertTrue(eligible_auto_alert({"kind": "delivery_test"}))

    def test_queued_old_unrelated_events_removed_but_owner_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = RadarState(path)
            state.set_owner(123)
            state.data["pending"] = [
                {"event_id": "a", "kind": "new_nft", "slug": "Test-1"},
                {"event_id": "b", "kind": "new_nft", "slug": "Test-2",
                 "original_participants": [RECIPIENT]},
                {"event_id": "c", "kind": "market_deal"},
                {"event_id": "d", "kind": "delivery_test"},
            ]
            state.save()
            self.assertEqual(filter_pending(state), 2)
            restored = RadarState(path)
            self.assertEqual(restored.owner_id, 123)
            self.assertEqual([v["event_id"] for v in restored.data["pending"]],
                             ["b", "d"])
            self.assertEqual(filter_pending(restored), 0)


if __name__ == "__main__":
    unittest.main()
