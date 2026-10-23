import unittest
from types import SimpleNamespace
from deal_watch import (
    deal_details, deal_opportunities, deal_snapshot, listed_prices, valid_gift_slug,
)


class StarsAmount:
    def __init__(self, amount, nanos=0):
        self.amount = amount
        self.nanos = nanos


class StarsTonAmount:
    def __init__(self, amount):
        self.amount = amount


class DealWatchTests(unittest.TestCase):
    def test_exact_slug_validation(self):
        self.assertTrue(valid_gift_slug("SpicedWine-108832"))
        self.assertFalse(valid_gift_slug("SpicedWine-0"))
        self.assertFalse(valid_gift_slug("badname"))
        self.assertFalse(valid_gift_slug("https://t.me/nft/SpicedWine-108832"))

    def test_hidden_owner_has_no_fake_contact_or_offer(self):
        gift = SimpleNamespace(slug="SpicedWine-108832", owner_id=None,
                               resell_amount=None)
        contact = {"owner_label": "𝒜𝒷𝑒𝓁", "owner_type": "name",
                   "owner_url": None, "owner_profile_url": None,
                   "offer_min_stars": None}
        state = deal_snapshot(gift, contact)
        self.assertFalse(state["offer_available"])
        self.assertFalse(state["resell_prices"])
        self.assertIn("не раскрывает", deal_details(state))
        self.assertEqual(deal_opportunities(state, state), [])

    def test_offer_requires_real_peer_id(self):
        gift = SimpleNamespace(slug="Example-4", owner_id=None, resell_amount=None)
        contact = {"owner_label": "Display name", "owner_type": "name",
                   "owner_url": None, "owner_profile_url": None,
                   "offer_min_stars": 300}
        state = deal_snapshot(gift, contact)
        self.assertFalse(state["offer_available"])

    def test_stars_and_ton_prices(self):
        gift = SimpleNamespace(resell_amount=[
            StarsAmount(100, 500_000_000),
            StarsTonAmount(2_500_000_000),
        ])
        self.assertEqual(listed_prices(gift), ["100.5 ⭐", "2.5 TON"])

    def test_notify_new_listing(self):
        old = {"resell_prices": [], "owner_url": None, "offer_available": False}
        new = {"resell_prices": ["200 ⭐"], "owner_url": None, "offer_available": False}
        changes = deal_opportunities(old, new)
        self.assertEqual(changes, ["Подарок выставлен на продажу или изменилась цена"])
        self.assertEqual(deal_opportunities(new, new), [])

    def test_notify_usable_owner_and_offer(self):
        old = {"resell_prices": [], "owner_url": None, "offer_available": False}
        new = {"resell_prices": [], "owner_url": "https://t.me/ActualUsername",
               "offer_available": True}
        changes = deal_opportunities(old, new)
        self.assertEqual(changes, [
            "Появился публичный профиль владельца",
            "Доступен официальный оффер"
        ])

    def test_notify_numeric_id_link_only_when_it_is_new(self):
        old = {"owner_url": None, "owner_profile_url": None}
        new = {"owner_url": None, "owner_profile_url": "tg://user?id=456"}
        self.assertEqual(deal_opportunities(old, new),
                         ["Telegram раскрыл ID владельца"])


if __name__ == "__main__":
    unittest.main()
