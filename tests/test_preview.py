"""Validate message-level NFT previews without connecting to Telegram."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

# main.py reads the bot token at import time; use a dummy value.
os.environ.setdefault("BOT_TOKEN", "dummy-token-for-offline-tests")
from main import message_for_gift, send_alert
from delivery import fallback_text


def nft_item(kind: str = "new_nft") -> dict:
    return {
        "kind": kind,
        "slug": "PartySparkler-189558",
        "title": "Party Sparkler",
        "number": 189558,
        "model": "Wand",
        "model_rarity": "2%",
        "backdrop": "Meadow",
        "backdrop_rarity": "8%",
        "pattern": "Star",
        "pattern_rarity": "1.4%",
        "owner_label": "@PublicOwner",
        "owner_type": "user",
        "owner_url": "https://t.me/PublicOwner",
        "owner_profile_url": "https://t.me/PublicOwner",
        "created_at": "2026-10-24T00:00:00+00:00",
    }


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_nft_contains_plain_url_first_and_enables_preview(self):
        item = nft_item()
        body = message_for_gift(item)
        link = "https://t.me/nft/PartySparkler-189558"
        self.assertIn(link, body)
        self.assertLess(body.index(link), body.index("https://t.me/PublicOwner"))
        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, body, item)
        kwargs = bot.send_message.await_args.kwargs
        self.assertTrue(kwargs["link_preview"])
        self.assertEqual(kwargs["parse_mode"], "html")
        self.assertTrue(kwargs["buttons"])

    async def test_rare_nft_uses_same_preview(self):
        item = nft_item("rare_nft")
        bot = SimpleNamespace(send_message=AsyncMock())
        body = message_for_gift(item)
        await send_alert(bot, 42, body, item)
        self.assertTrue(bot.send_message.await_args.kwargs["link_preview"])
        self.assertIn("https://t.me/nft/PartySparkler-189558", body)

    async def test_non_nft_message_does_not_show_link_preview(self):
        item = {
            "kind": "upgrade_unlocked",
            "title": "New Gift",
            "gift_id": "123",
            "upgrade_stars": 25,
        }
        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, message_for_gift(item), item)
        self.assertFalse(bot.send_message.await_args.kwargs["link_preview"])

    async def test_delivery_probe_does_not_show_preview(self):
        item = {"kind": "delivery_test"}
        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, message_for_gift(item), item)
        self.assertFalse(bot.send_message.await_args.kwargs["link_preview"])

    def test_plain_fallback_contains_exact_link(self):
        body = fallback_text(nft_item())
        self.assertIn("https://t.me/nft/PartySparkler-189558", body)


if __name__ == "__main__":
    unittest.main()
