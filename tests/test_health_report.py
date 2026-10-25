"""Health reports distinguish quiet markets from broken scanner sources."""
import unittest
from datetime import datetime, timedelta, timezone

from health_report import age, health_issues, diagnostic_text


class HealthReportTests(unittest.TestCase):
    def test_quiet_market_with_healthy_sources_is_not_a_failure(self):
        now = datetime.now(timezone.utc).isoformat()
        nft = {"candidates": 50, "last_catalog": now, "last_success": now}
        live = {"status": "подключено", "last_stats": now, "subscribed_kinds": ["new", "stats"]}
        self.assertEqual(health_issues(nft, live, {}), [])

    def test_new_feed_disabled_is_reported(self):
        now = datetime.now(timezone.utc).isoformat()
        issues = health_issues(
            {"candidates": 50, "last_catalog": now, "last_success": now},
            {"status": "подключено", "disabled_kinds": ["new"]}, {},
        )
        self.assertTrue(any("new" in message or "новых NFT" in message for message in issues))

    def test_stale_scanner_is_reported(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        issues = health_issues(
            {"last_catalog": old, "last_success": old, "candidates": 10},
            {"status": "не запускался"}, {},
        )
        self.assertTrue(any("20 минут" in message for message in issues))

    def test_pause_and_explicit_disable_avoid_false_alarms(self):
        self.assertEqual(health_issues({}, {}, {"paused": True}), [])
        self.assertEqual(health_issues({}, {}, {}, scanner_enabled=False, live_enabled=False), [])

    def test_private_report_escapes_source_errors(self):
        report = diagnostic_text(
            {"last_error": "<example>&", "candidates": 2},
            {"status": "подключено"}, {}, {}, {}, 1,
        )
        self.assertIn("&lt;example&gt;&amp;", report)
        self.assertIn("/testdelivery", report)


if __name__ == "__main__":
    unittest.main()
