import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "offline-dummy-token")
import main
from live_market import normalize_live_event
from radar import RadarState
from delivery import fallback_text


def upgrade(number=234):
    return normalize_live_event({
        "type": "event", "event": "new", "at": "2026-10-24T00:00:00Z",
        "gift": {"slug": "PlushPepe", "num": number,
                 "title": "Plush Pepe", "model": "Regular",
                 "backdrop": "Pink", "pattern": "Rose"},
    })


class IntegratedLiveEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_state = main.state
        main.state = RadarState(Path(self.dir.name) / "state.json")
        main.state.set_owner(12)
        main.LIVE_COUNTERS["minute_bucket"] = None
        main.LIVE_COUNTERS["minute_count"] = 0

    def tearDown(self):
        main.state = self.old_state
        self.dir.cleanup()

    async def test_guest_live_mint_delivers_using_persistent_queue(self):
        event = upgrade()
        with patch.object(main, "deliver_pending", new_callable=AsyncMock) as wake:
            await main.live_market_event(event)
            self.assertEqual(len(main.state.data["pending"]), 1)
            self.assertEqual(main.state.data["pending"][0]["slug"], "PlushPepe-234")
            wake.assert_awaited_once()
        duplicate = main.state.record_nft_once(dict(
            main.state.data["pending"][0], kind="rare_nft"
        ))
        self.assertFalse(duplicate)
        restored = RadarState(Path(self.dir.name) / "state.json")
        self.assertFalse(restored.record_nft_once(
            {"kind": "new_nft", "slug": "PlushPepe-234"}
        ))

    async def test_price_only_notifies_with_sufficient_observed_sales(self):
        values = [3_000_000_000, 4_000_000_000, 5_000_000_000]
        for i, value in enumerate(values):
            sale = normalize_live_event({
                "type": "event", "event": "sale",
                "at": f"2026-10-24T00:00:0{i}Z",
                "gift": {"slug": "PlushPepe", "num": i + 1, "model": "Rare"},
                "price": {"amount": value, "currency": "gram"},
            })
            await main.live_market_event(sale)
        self.assertEqual(len(main.state.data["pending"]), 0)
        listing = normalize_live_event({
            "type": "event", "event": "listing",
            "at": "2026-10-24T00:01:00Z", "market": "portals",
            "gift": {"slug": "PlushPepe", "num": 1234, "model": "Rare"},
            "price": {"amount": 2_000_000_000, "currency": "gram"},
        })
        with patch.object(main, "deliver_pending", new_callable=AsyncMock) as wake:
            await main.live_market_event(listing)
            self.assertEqual(main.state.data["pending"][0]["kind"], "market_deal")
            alert = main.state.data["pending"][0]
            self.assertIn("https://t.me/nft/PlushPepe-1234", main.message_for_gift(alert))
            self.assertIn("https://t.me/nft/PlushPepe-1234", fallback_text(alert))
            wake.assert_awaited_once()

    async def test_disabled_all_nft_mode_does_not_push_live_new(self):
        main.state.data["notify_all_nfts"] = False
        await main.live_market_event(upgrade(240))
        self.assertEqual(main.state.data["pending"], [])


if __name__ == "__main__":
    unittest.main()
