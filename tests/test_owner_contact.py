import unittest
from types import SimpleNamespace

from owner_contact import offer_text, owner_contact, public_username


class PeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class PeerChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id


class OwnerContactTests(unittest.TestCase):
    def test_public_user_username_creates_safe_contact_link(self):
        collectible = SimpleNamespace(owner_id=PeerUser(345), owner_name=None,
                                      owner_address=None, offer_min_stars=200)
        response = SimpleNamespace(users=[
            SimpleNamespace(id=345, username="GoodSeller_5", first_name="Seller"),
            SimpleNamespace(id=55, username="WrongUser", first_name="Wrong")],
            chats=[])
        owner = owner_contact(collectible, response)
        self.assertEqual(owner["owner_label"], "@GoodSeller_5")
        self.assertEqual(owner["owner_url"], "https://t.me/GoodSeller_5")
        self.assertEqual(owner["owner_type"], "user")
        self.assertEqual(owner["offer_min_stars"], 200)

    def test_no_public_username_creates_no_link(self):
        collectible = SimpleNamespace(owner_id=PeerUser(345), owner_address=None,
                                      owner_name=None, offer_min_stars=None)
        response = SimpleNamespace(users=[
            SimpleNamespace(id=345, username=None, first_name="Private")
        ], chats=[])
        owner = owner_contact(collectible, response)
        self.assertIsNone(owner["owner_url"])
        self.assertIn("без публичного", owner["owner_label"])

    def test_only_active_usernames_accepted(self):
        user = SimpleNamespace(username=None, usernames=[
            SimpleNamespace(username="Inactive42", active=False),
            SimpleNamespace(username="ActiveName", active=True),
        ])
        self.assertEqual(public_username(user), "ActiveName")

    def test_wallet_is_not_mistaken_for_telegram_account(self):
        gift = SimpleNamespace(owner_id=None, owner_address="EQsomeTONaddress",
                               owner_name=None, offer_min_stars=None)
        contact = owner_contact(gift, SimpleNamespace(users=[], chats=[]))
        self.assertEqual(contact["owner_type"], "wallet")
        self.assertIsNone(contact["owner_url"])

    def test_owner_name_is_nonactionable(self):
        gift = SimpleNamespace(owner_id=None, owner_address=None,
                               owner_name="Hidden Gift Lover", offer_min_stars=None)
        contact = owner_contact(gift, SimpleNamespace(users=[], chats=[]))
        self.assertEqual(contact["owner_label"], "Hidden Gift Lover")
        self.assertIsNone(contact["owner_url"])

    def test_channel_link_is_not_direct_user(self):
        gift = SimpleNamespace(owner_id=PeerChannel(42), owner_address=None,
                               owner_name=None, offer_min_stars=None)
        res = SimpleNamespace(users=[], chats=[
            SimpleNamespace(id=42, username="MyGiftChannel", title="My channel")
        ])
        contact = owner_contact(gift, res)
        self.assertEqual(contact["owner_type"], "channel")
        self.assertEqual(contact["owner_url"], "https://t.me/MyGiftChannel")

    def test_malformed_username_is_not_linked(self):
        gift = SimpleNamespace(owner_id=PeerUser(10), owner_address=None,
                               owner_name=None, offer_min_stars=None)
        response = SimpleNamespace(users=[
            SimpleNamespace(id=10, username="oops?foo=bad", first_name="Private")
        ], chats=[])
        contact = owner_contact(gift, response)
        self.assertIsNone(contact["owner_url"])

    def test_offer_draft_uses_exact_gift_and_stars(self):
        gift = SimpleNamespace(title="Plush Pepe", num=123, slug="PlushPepe-123")
        msg = offer_text(gift, 1500)
        self.assertIn("Plush Pepe #123", msg)
        self.assertIn("1500 ⭐", msg)
        self.assertIn("https://t.me/nft/PlushPepe-123", msg)
        self.assertIn("официальный оффер", msg.lower())


if __name__ == "__main__":
    unittest.main()
