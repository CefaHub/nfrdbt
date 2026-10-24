import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commercial import CommercialStore, ADMIN_ID
from radar import RadarState


class SalesLinksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "commercial.sqlite3"
        self.store = CommercialStore(self.path)

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_fixed_admin_only_and_unlimited_links_over_many_batches(self):
        self.assertEqual(ADMIN_ID, 8714018880)
        self.assertTrue(self.store.is_subscribed(ADMIN_ID))
        self.assertEqual(len(self.store.create_links(50)), 50)
        self.assertEqual(len(self.store.create_links(50)), 50)
        self.assertEqual(self.store.stats()["unused_links"], 100)
        self.assertEqual(self.store.stats()["active_users"], 1)
        with self.assertRaises(ValueError):
            self.store.create_links(51)

    def test_codes_are_hashed_stored_without_expiration_and_one_use(self):
        code = self.store.create_links(1)[0]
        self.assertNotIn(code, self.path.read_bytes().decode("latin-1", errors="ignore"))
        self.assertEqual(self.store.redeem("invalid", 200), "invalid")
        self.assertEqual(self.store.redeem(code, 200), "activated")
        self.assertEqual(self.store.redeem(code, 201), "invalid")
        self.assertEqual(self.store.stats()["redeemed_links"], 1)
        self.assertTrue(self.store.is_subscribed(200))
        self.store.deactivate(200)
        self.assertFalse(self.store.is_subscribed(200))
        self.assertEqual(self.store.redeem(code, 200), "existing")
        self.assertTrue(self.store.is_subscribed(200))

    def test_day_pass_is_24_hours_from_activation_not_from_link_generation(self):
        with patch("commercial.time.time", return_value=100_000):
            link = self.store.create_links(1, duration_days=1)[0]
            self.assertEqual(self.store.stats()["trial_links"], 1)
        with patch("commercial.time.time", return_value=200_000):
            self.assertEqual(self.store.redeem(link, 200), "activated")
            self.assertEqual(self.store.license_info(200)["expires_at"], 286_400)
            self.assertTrue(self.store.is_subscribed(200))
        with patch("commercial.time.time", return_value=286_399):
            self.assertTrue(self.store.is_subscribed(200))
        with patch("commercial.time.time", return_value=286_400):
            self.assertFalse(self.store.is_subscribed(200))
            self.assertTrue(self.store.license_info(200)["expired"])
            self.assertFalse(self.store.reactivate(200))
            self.assertEqual(self.store.stats()["trial_expired"], 1)

    def test_expired_buyer_cannot_receive_notifications_or_reactivate_without_new_link(self):
        with patch("commercial.time.time", return_value=1_000_000):
            self.store.redeem(self.store.create_links(1, duration_days=1)[0], 200)
            self.store.publish({"event_id": "e1", "kind": "new_nft", "slug": "G-1"})
            self.assertEqual(self.store.delivery_backlog(), 2)
        with patch("commercial.time.time", return_value=1_086_400):
            self.assertFalse(self.store.is_subscribed(200))
            self.assertEqual(self.store.delivery_backlog(), 1)
            self.assertEqual(self.store.next_delivery()[0], ADMIN_ID)
            self.assertFalse(self.store.reactivate(200))
            self.store.ack(ADMIN_ID, self.store.next_delivery()[1])
            self.assertIsNone(self.store.next_delivery())

    def test_day_renewal_and_lifetime_upgrade_never_downgrade(self):
        with patch("commercial.time.time", return_value=5_000):
            self.assertEqual(self.store.redeem(
                self.store.create_links(1, duration_days=1)[0], 200
            ), "activated")
            self.assertEqual(self.store.license_info(200)["expires_at"], 91_400)
        with patch("commercial.time.time", return_value=6_000):
            self.assertEqual(self.store.redeem(
                self.store.create_links(1, duration_days=1)[0], 200
            ), "extended")
            self.assertEqual(self.store.license_info(200)["expires_at"], 177_800)
            self.assertEqual(self.store.redeem(
                self.store.create_links(1)[0], 200
            ), "upgraded")
            self.assertIsNone(self.store.license_info(200)["expires_at"])
            daily = self.store.create_links(1, duration_days=1)[0]
            self.assertEqual(self.store.redeem(daily, 200), "existing")
            self.assertEqual(self.store.stats()["trial_links"], 1)
            self.assertEqual(self.store.redeem(daily, 201), "activated")
            self.assertIsNone(self.store.license_info(200)["expires_at"])

    def test_expired_pass_renewal_skips_old_alerts(self):
        with patch("commercial.time.time", return_value=1_000):
            self.store.redeem(self.store.create_links(1, duration_days=1)[0], 200)
        with patch("commercial.time.time", return_value=88_000):
            self.store.publish({"event_id": "missed", "kind": "new_nft", "slug": "G-1"})
            self.assertEqual(self.store.redeem(
                self.store.create_links(1, duration_days=1)[0], 200
            ), "extended")
            # Customer may only see NEW alerts after renewing an expired pass.
            work = self.store.next_delivery()
            self.assertEqual(work[0], ADMIN_ID)
            self.store.ack(ADMIN_ID, work[1])
            self.assertIsNone(self.store.next_delivery())

    def test_existing_database_migrates_without_expiring_lifetime(self):
        import sqlite3
        legacy_path = Path(self.tmp.name) / "before-update.sqlite3"
        with sqlite3.connect(legacy_path) as conn:
            conn.executescript("""
                CREATE TABLE users (
                    chat_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
                    cursor INTEGER NOT NULL DEFAULT 0,
                    retry_after INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    joined_at INTEGER NOT NULL
                );
                CREATE TABLE invites (
                    code_hash TEXT PRIMARY KEY, issued_at INTEGER NOT NULL,
                    redeemed_by INTEGER, redeemed_at INTEGER
                );
                INSERT INTO users(chat_id, active, cursor, joined_at)
                    VALUES(123,1,0,1000);
            """)
        recovered = CommercialStore(legacy_path)
        try:
            self.assertTrue(recovered.is_subscribed(123))
            self.assertIsNone(recovered.license_info(123)["expires_at"])
            self.assertTrue(recovered.is_subscribed(ADMIN_ID))
            self.assertEqual(recovered.stats()["lifetime_links"], 0)
            recovered.create_links(1, duration_days=1)
            self.assertEqual(recovered.stats()["trial_links"], 1)
        finally:
            recovered.conn.close()

    def test_invalid_duration_or_invalid_link_no_access(self):
        with self.assertRaises(ValueError):
            self.store.create_links(1, duration_days=2)
        with self.assertRaises(ValueError):
            self.store.create_links(1, duration_days=0)
        self.assertEqual(self.store.redeem("bogus", 200), "invalid")
        self.assertFalse(self.store.is_subscribed(200))

    def test_broadcast_does_not_copy_nft_for_every_subscriber(self):
        codes = self.store.create_links(3)
        for uid, code in zip((200, 201, 202), codes):
            self.assertEqual(self.store.redeem(code, uid), "activated")
        item = {"event_id": "event-1", "kind": "new_nft",
                "slug": "SnakeBox-123", "title": "Snake Box",
                "targets": [100, 200, 201, 202]}
        self.assertTrue(self.store.publish(item))
        self.assertFalse(self.store.publish(item))
        self.assertEqual(self.store.stats()["alerts"], 1)
        delivered = []
        for _ in range(4):
            chat_id, seq, payload = self.store.next_delivery()
            delivered.append(chat_id)
            self.assertEqual(seq, 1)
            self.assertEqual(payload["slug"], "SnakeBox-123")
            self.assertNotIn("targets", payload)
            self.store.ack(chat_id, seq)
        self.assertEqual(set(delivered), {ADMIN_ID, 200, 201, 202})
        self.assertEqual(self.store.delivery_backlog(), 0)

    def test_new_customer_never_receives_old_alerts(self):
        self.store.publish({"event_id": "old", "kind": "new_nft",
                            "slug": "Gift-1"})
        code = self.store.create_links(1)[0]
        self.assertEqual(self.store.redeem(code, 200), "activated")
        item = self.store.next_delivery()
        self.assertEqual(item[0], ADMIN_ID)
        self.store.ack(item[0], item[1])
        self.assertIsNone(self.store.next_delivery())
        self.store.publish({"event_id": "new", "kind": "new_nft",
                            "slug": "Gift-2"})
        ids = set()
        for _ in range(2):
            chat_id, seq, payload = self.store.next_delivery()
            self.assertEqual(payload["slug"], "Gift-2")
            ids.add(chat_id)
            self.store.ack(chat_id, seq)
        self.assertEqual(ids, {ADMIN_ID, 200})

    def test_failed_subscriber_is_deferred_without_blocking_broadcast(self):
        code = self.store.create_links(1)[0]
        self.store.redeem(code, 200)
        self.store.publish({"event_id": "e", "kind": "new_nft", "slug": "A-1"})
        chat_id, _, _ = self.store.next_delivery()
        delay = self.store.retry_later(chat_id)
        self.assertGreaterEqual(delay, 2)
        next_item = self.store.next_delivery()
        self.assertIsNotNone(next_item)
        self.assertNotEqual(next_item[0], chat_id)

    def test_legacy_migration_preserves_customers_but_retires_ownership(self):
        state = RadarState(Path(self.tmp.name) / "state.json")
        state.set_owner(111)
        state.data["subscribers"] = [222]
        state.data["invites"] = [{"hash": "legacy"}]
        state.record_rare_nft({"kind": "new_nft", "slug": "A-1"})
        self.store.migrate_legacy(state)
        self.assertEqual(state.owner_id, ADMIN_ID)
        self.assertEqual(state.data["pending"], [])
        self.assertEqual(state.data["subscribers"], [])
        self.assertEqual(state.data["invites"], [])
        self.assertEqual(self.store.stats()["alerts"], 1)
        self.assertTrue(self.store.is_subscribed(111))
        self.assertTrue(self.store.is_subscribed(222))
        self.store.migrate_legacy(state)
        self.assertEqual(self.store.stats()["alerts"], 1)

    def test_can_handle_thousands_without_per_alert_fanout(self):
        now = 1234
        with self.store.conn:
            self.store.conn.executemany(
                "INSERT INTO users(chat_id, cursor, joined_at) VALUES(?,?,?)",
                [(10_000 + i, 0, now) for i in range(1500)]
            )
        self.store.publish({"event_id": "volume", "kind": "new_nft",
                            "slug": "A-2"})
        self.assertEqual(self.store.stats()["alerts"], 1)
        self.assertEqual(self.store.stats()["active_users"], 1501)
        self.assertEqual(self.store.delivery_backlog(), 1501)


if __name__ == "__main__":
    unittest.main()
