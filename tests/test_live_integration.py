import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

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


class PeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class StarGiftAttributeOriginalDetails:
    def __init__(self, recipient_id=None):
        self.sender_id = None
        self.recipient_id = recipient_id


class FakeGiftUser:
    def __init__(self, slug, has_recipient=True):
        self.slug = slug
        self.has_recipient = has_recipient

    async def __call__(self, request):
        attrs = ([StarGiftAttributeOriginalDetails(PeerUser(456))]
                 if self.has_recipient else [])
        return SimpleNamespace(
            gift=SimpleNamespace(slug=self.slug, attributes=attrs,
                                 crafted=False, owner_id=None),
            users=[SimpleNamespace(id=456, username="Recipient456")],
            chats=[],
        )

    async def get_entity(self, peer):
        return SimpleNamespace(id=peer.user_id, username="Recipient456")


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
            await main.live_market_event(event, FakeGiftUser("PlushPepe-234"))
            self.assertEqual(len(main.state.data["pending"]), 1)
            self.assertEqual(main.state.data["pending"][0]["slug"], "PlushPepe-234")
            self.assertEqual(main.state.data["pending"][0]["original_participants"][0]["role"], "recipient")
            wake.assert_awaited_once()
        duplicate = main.state.record_nft_once(dict(
            main.state.data["pending"][0], kind="rare_nft"
        ))
        self.assertFalse(duplicate)
        restored = RadarState(Path(self.dir.name) / "state.json")
        self.assertFalse(restored.record_nft_once(
            {"kind": "new_nft", "slug": "PlushPepe-234"}
        ))

    async def test_live_new_without_recipient_does_not_notify(self):
        with patch.object(main, "deliver_pending", new_callable=AsyncMock) as wake:
            await main.live_market_event(
                upgrade(444), FakeGiftUser("PlushPepe-444", has_recipient=False)
            )
            self.assertEqual(main.state.data["pending"], [])
            wake.assert_not_awaited()

    async def test_live_api_failure_does_not_enqueue_an_unverified_gift(self):
        class BrokenUser:
            async def __call__(self, request):
                raise ValueError("Telegram temporarily unavailable")
        await main.live_market_event(upgrade(445), BrokenUser())
        self.assertEqual(main.state.data["pending"], [])

    async def test_disabled_all_nft_mode_does_not_push_live_new(self):
        main.state.data["notify_all_nfts"] = False
        await main.live_market_event(upgrade(240))
        self.assertEqual(main.state.data["pending"], [])


if __name__ == "__main__":
    unittest.main()
