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
        "owner": {"kind": "user", "id": 99,
                  "label": "@CurrentOwner99", "url": "https://t.me/CurrentOwner99"},
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
        self.assertIn("Владелец: ", text)
        self.assertIn("https://t.me/CurrentOwner99", text)
        self.assertNotIn("Кому подарили:", text)
        self.assertNotIn("https://t.me/Recipient72", text)
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
        self.assertEqual(kwargs["buttons"][1][0].text, "Профиль владельца")
        self.assertIn("https://t.me/CurrentOwner99", text)

    async def test_owner_without_public_profile_has_no_misleading_button(self):
        gift = item()
        gift["owner"] = {
            "kind": "user", "id": 99,
            "label": "Telegram ID 99", "url": None,
        }
        msg = message_for_gift(gift)
        self.assertIn("Владелец: Telegram ID 99", msg)
        self.assertEqual(len(nft_buttons(gift)), 1)
        self.assertNotIn("Recipient72", msg)

    async def test_legacy_notifications_do_not_mislabel_original_recipient(self):
        gift = item()
        gift.pop("owner")
        self.assertIn("Владелец: Не указан", message_for_gift(gift))
        self.assertEqual(len(nft_buttons(gift)), 1)

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
        message = fallback_text(item())
        self.assertIn("https://t.me/nft/PartySparkler-189558", message)
        self.assertIn("Владелец: @CurrentOwner99", message)
        self.assertNotIn("Кому подарили", message)
        self.assertNotIn("Recipient72", message)


if __name__ == "__main__":
    unittest.main()
