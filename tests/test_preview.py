"""Message and Telegram preview regression tests for the minimal NFT radar."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "offline-dummy-token")
from main import message_for_gift, send_alert, nft_buttons
from delivery import fallback_text


def item(kind="new_nft", recipient=True):
    data = {
        "kind": kind, "slug": "PartySparkler-189558",
        "title": "Party Sparkler", "number": 189558,
        "model": "Wand", "backdrop": "Meadow",
        "pattern": "Star", "created_at": "2026-10-24T00:00:00+00:00",
        "original_participants": [
            {"role": "sender", "type": "user", "id": 51,
             "label": "@Sender51", "url": "https://t.me/Sender51"},
        ],
    }
    if recipient:
        data["original_participants"].append({
            "role": "recipient", "type": "user", "id": 72,
            "label": "@Recipient72", "url": "https://t.me/Recipient72",
        })
    return data


class MinimalTelegramUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_has_precise_link_and_only_essential_fields(self):
        gift = item()
        text = message_for_gift(gift)
        self.assertIn("<b>Party Sparkler #189558</b>", text)
        self.assertIn("https://t.me/nft/PartySparkler-189558", text)
        self.assertIn("Кому подарили:", text)
        self.assertIn("https://t.me/Recipient72", text)
        self.assertNotIn("https://t.me/Sender51", text)
        self.assertNotIn("Черновик", text)
        self.assertNotIn("/watch", text)
        self.assertNotIn("Оффер", text)
        self.assertNotIn("ПРОВЕРЬ", text)
        self.assertNotIn("🔥", text)

        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, text, gift)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["parse_mode"], "html")
        self.assertTrue(kwargs["link_preview"])
        self.assertEqual(len(kwargs["buttons"]), 2)
        self.assertEqual(kwargs["buttons"][0][0].text, "Открыть подарок")
        self.assertEqual(kwargs["buttons"][1][0].text, "Профиль получателя")

    async def test_id_only_recipient_stays_in_text_without_invalid_url_button(self):
        gift = item()
        gift["original_participants"][-1] = {
            "role": "recipient", "type": "user", "id": 72,
            "label": "Telegram ID 72", "url": "tg://user?id=72",
        }
        msg = message_for_gift(gift)
        self.assertIn("ID 72", msg)
        self.assertEqual(len(nft_buttons(gift)), 1)
        self.assertIn("Кому подарили", msg)

    async def test_rare_and_regular_have_same_simple_layout(self):
        normal = message_for_gift(item())
        rare = message_for_gift(item("rare_nft"))
        self.assertEqual(normal, rare)

    async def test_delivery_diagnostic_is_plain(self):
        gift = {"kind": "delivery_test"}
        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, message_for_gift(gift), gift)
        self.assertFalse(bot.send_message.await_args.kwargs["link_preview"])
        self.assertIsNone(bot.send_message.await_args.kwargs["buttons"])

    async def test_no_automatic_preview_for_unrelated_text(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await send_alert(bot, 42, "Update", {"kind": "upgrade_unlocked"})
        self.assertFalse(bot.send_message.await_args.kwargs["link_preview"])

    def test_fallback_retains_nft_url(self):
        self.assertIn("https://t.me/nft/PartySparkler-189558", fallback_text(item()))


if __name__ == "__main__":
    unittest.main()
