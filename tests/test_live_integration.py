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
from commercial import CommercialStore, ADMIN_ID
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
                                 crafted=False, owner_id=PeerUser(999)),
            users=[SimpleNamespace(id=456, username="Recipient456"),
                   SimpleNamespace(id=999, username="RealCurrentOwner")],
            chats=[],
        )

    async def get_entity(self, peer):
        return SimpleNamespace(id=peer.user_id, username="Recipient456")


class IntegratedLiveEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_state = main.state
        self.old_store = main.store
        main.state = RadarState(Path(self.dir.name) / "state.json")
        main.state.set_owner(ADMIN_ID)
        main.store = CommercialStore(Path(self.dir.name) / "commercial.sqlite3")
        main.LIVE_COUNTERS["minute_bucket"] = None
        main.LIVE_COUNTERS["minute_count"] = 0

    def tearDown(self):
        main.state = self.old_state
        main.store.conn.close()
        main.store = self.old_store
        self.dir.cleanup()

    async def test_guest_live_mint_delivers_using_persistent_queue(self):
        event = upgrade()
        with patch.object(main, "deliver_pending", new_callable=AsyncMock) as wake:
            await main.live_market_event(event, FakeGiftUser("PlushPepe-234"))
            self.assertEqual(main.state.data["pending"], [])
            sent = main.store.next_delivery()
            self.assertIsNotNone(sent)
            self.assertEqual(sent[0], ADMIN_ID)
            self.assertEqual(sent[2]["slug"], "PlushPepe-234")
            self.assertEqual(sent[2]["original_participants"][0]["role"], "recipient")
            self.assertEqual(sent[2]["owner"]["label"], "@RealCurrentOwner")
            self.assertEqual(sent[2]["owner"]["url"], "https://t.me/RealCurrentOwner")
            wake.assert_awaited_once()
        duplicate = main.state.record_nft_once(dict(
            sent[2], kind="rare_nft"
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

    async def test_deferred_recipient_recheck_recovers_and_queues(self):
        # Mint discovered without original details; recheck later sees them.
        from recipient_recheck import schedule_review
        slug = "PlushPepe-500"
        self.assertTrue(schedule_review(main.state.data, slug, now=0))
        main.state.save()
        checked = FakeGiftUser(slug, has_recipient=True)
        delivered = __import__("asyncio").Event()

        async def notify(_bot):
            delivered.set()

        with patch.object(main, "deliver_pending", side_effect=notify):
            worker = __import__("asyncio").create_task(
                main.retry_new_recipient_details(checked, None)
            )
            try:
                await __import__("asyncio").wait_for(delivered.wait(), timeout=2)
                self.assertEqual(main.state.data["pending"], [])
                self.assertEqual(main.store.next_delivery()[2]["slug"], slug)
                self.assertNotIn(slug, main.state.data.get("recipient_reviews") or {})
            finally:
                worker.cancel()
                with self.assertRaises(__import__("asyncio").CancelledError):
                    await worker

    async def test_deferred_no_recipient_remains_pending_for_later_retry(self):
        from recipient_recheck import schedule_review
        slug = "PlushPepe-501"
        self.assertTrue(schedule_review(main.state.data, slug, now=0))
        main.state.save()
        visitor = FakeGiftUser(slug, has_recipient=False)
        with patch.object(main, "deliver_pending", new_callable=AsyncMock) as notify:
            worker = __import__("asyncio").create_task(
                main.retry_new_recipient_details(visitor, None)
            )
            try:
                for _ in range(30):
                    await __import__("asyncio").sleep(0.01)
                    data = main.state.data["recipient_reviews"].get(slug)
                    if data and data["attempts"] >= 1:
                        break
                self.assertEqual(main.state.data["pending"], [])
                self.assertIn(slug, main.state.data["recipient_reviews"])
                self.assertEqual(main.state.data["recipient_reviews"][slug]["attempts"], 1)
                notify.assert_not_awaited()
            finally:
                worker.cancel()
                with self.assertRaises(__import__("asyncio").CancelledError):
                    await worker

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
