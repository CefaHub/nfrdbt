"""Aggregate audience analytics for the NFT Gift Radar admin panel."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commercial import ADMIN_ID, CommercialStore


class ActivityStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "commercial.sqlite3"
        self.store = CommercialStore(self.db)

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_new_users_and_activity_windows_are_persistent(self):
        with patch("commercial.time.time", return_value=1_000_000):
            self.store.record_activity(101)
            self.store.record_activity(101)  # one user, not two messages
            self.store.record_activity(ADMIN_ID)
        with patch("commercial.time.time", return_value=1_000_000 + 3600):
            self.store.record_activity(102)
        with patch("commercial.time.time", return_value=1_000_000 + 86400 + 1):
            self.store.record_activity(103)
            s = self.store.stats()
        self.assertEqual(s["known_users"], 3)
        self.assertEqual(s["active_5m"], 1)
        self.assertEqual(s["active_24h"], 2)
        self.assertEqual(s["active_7d"], 3)
        self.assertEqual(s["subscribed_users"], 0)

        self.store.conn.close()
        self.store = CommercialStore(self.db)
        with patch("commercial.time.time", return_value=1_000_000 + 86400 + 1):
            self.assertEqual(self.store.stats()["active_24h"], 2)

    def test_licenses_count_even_before_activity_tracking_is_installed(self):
        with patch("commercial.time.time", return_value=1000):
            token = self.store.create_links(1)[0]
            self.assertEqual(self.store.redeem(token, 201), "activated")
            summary = self.store.stats()
        self.assertEqual(summary["known_users"], 1)
        self.assertEqual(summary["subscribed_users"], 1)
        self.assertEqual(summary["active_7d"], 0)
        with patch("commercial.time.time", return_value=1100):
            self.store.record_activity(201)
            self.store.record_activity(301)  # a visitor with no license
            summary = self.store.stats()
        self.assertEqual(summary["known_users"], 2)
        self.assertEqual(summary["active_5m"], 2)
        self.assertEqual(summary["subscribed_users"], 1)

    def test_block_is_counted_only_if_explicit_and_clears_on_contact(self):
        with patch("commercial.time.time", return_value=150_000):
            self.store.record_activity(200)
            self.store.mark_blocked(200)
            self.store.mark_blocked(201)  # could not deliver to a licensed user
            self.store.mark_blocked(ADMIN_ID)
            summary = self.store.stats()
            self.assertEqual(summary["blocked_users"], 2)
            self.assertEqual(summary["active_5m"], 0)
            self.assertEqual(summary["known_users"], 2)
            self.store.record_activity(200)
            summary = self.store.stats()
        self.assertEqual(summary["blocked_users"], 1)
        self.assertEqual(summary["active_5m"], 1)

    def test_stopped_and_expired_subscriptions_not_counted(self):
        with patch("commercial.time.time", return_value=50_000):
            daily = self.store.create_links(1, duration_days=1)[0]
            lifetime = self.store.create_links(1)[0]
            self.store.redeem(daily, 11)
            self.store.redeem(lifetime, 12)
            self.assertEqual(self.store.stats()["subscribed_users"], 2)
            self.store.deactivate(12)
            self.assertEqual(self.store.stats()["subscribed_users"], 1)
        with patch("commercial.time.time", return_value=50_000 + 86400):
            self.assertEqual(self.store.stats()["subscribed_users"], 0)
            self.assertEqual(self.store.stats()["known_users"], 2)


if __name__ == "__main__":
    unittest.main()
