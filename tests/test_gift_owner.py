"""Current ownership must never be inferred from gift history or hosting."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gift_owner import current_owner, resolve_current_owner, profile_url


class PeerUser:
    def __init__(self, user_id): self.user_id = user_id


class PeerChannel:
    def __init__(self, channel_id): self.channel_id = channel_id


class PeerChat:
    def __init__(self, chat_id): self.chat_id = chat_id


class CurrentOwnerTests(unittest.IsolatedAsyncioTestCase):
    def test_different_owner_and_original_recipient(self):
        gift = SimpleNamespace(owner_id=PeerUser(99), attributes=[
            SimpleNamespace(recipient_id=PeerUser(77)),
        ])
        response = SimpleNamespace(users=[
            SimpleNamespace(id=77, username="OldRecipient"),
            SimpleNamespace(id=99, username="RealOwner"),
        ], chats=[])
        owner = current_owner(gift, response)
        self.assertEqual(owner["label"], "@RealOwner")
        self.assertEqual(profile_url(owner), "https://t.me/RealOwner")
        self.assertNotIn("OldRecipient", str(owner))

    def test_owner_id_preferred_over_nonowner_host(self):
        gift = SimpleNamespace(
            owner_id=PeerUser(123), host_id=PeerUser(456),
            owner_name="Just a label",
        )
        response = SimpleNamespace(users=[
            SimpleNamespace(id=456, username="HostOnly"),
            SimpleNamespace(id=123, username="ActualOwner"),
        ])
        owner = current_owner(gift, response)
        self.assertEqual(owner["label"], "@ActualOwner")

    def test_hidden_current_owner_uses_verified_id_deep_link(self):
        gift = SimpleNamespace(owner_id=PeerUser(99))
        response = SimpleNamespace(users=[
            SimpleNamespace(id=99, username=None, first_name="Private"),
        ])
        owner = current_owner(gift, response)
        self.assertEqual(owner["label"], "Private")
        self.assertEqual(profile_url(owner), "tg://user?id=99")

    def test_invalid_or_unverified_owner_does_not_link_to_recipient(self):
        self.assertIsNone(profile_url({"kind": "name", "label": "Unknown", "id": 77}))
        self.assertIsNone(profile_url({"kind": "user", "id": -5, "url": None}))
        self.assertIsNone(profile_url(None))

    def test_ownership_from_channel(self):
        gift = SimpleNamespace(owner_id=PeerChannel(7))
        response = SimpleNamespace(
            users=[], chats=[SimpleNamespace(id=7, username="OpenChannel")]
        )
        self.assertEqual(
            profile_url(current_owner(gift, response)),
            "https://t.me/OpenChannel",
        )

    def test_wallet_is_distinct_from_host_and_no_telegram_button(self):
        gift = SimpleNamespace(
            owner_id=None, owner_address="EQ" + "5" * 44,
            host_id=PeerUser(33),
        )
        owner = current_owner(gift, SimpleNamespace(
            users=[SimpleNamespace(id=33, username="NotOwner")]
        ))
        self.assertEqual(owner["kind"], "wallet")
        self.assertIn("TON ", owner["label"])
        self.assertIsNone(profile_url(owner))

    def test_owner_name_only_no_profile(self):
        gift = SimpleNamespace(owner_id=None, owner_name="Anonymous seller")
        owner = current_owner(gift, SimpleNamespace())
        self.assertEqual(owner["label"], "Anonymous seller")
        self.assertIsNone(profile_url(owner))

    def test_no_owner_is_unknown_not_original_recipient(self):
        gift = SimpleNamespace(owner_id=None, attributes=[])
        owner = current_owner(gift, SimpleNamespace())
        self.assertEqual(owner["label"], "Не указан")
        self.assertIsNone(profile_url(owner))

    async def test_lookup_missing_username_by_owner_peer(self):
        peer = PeerUser(99)
        gift = SimpleNamespace(owner_id=peer)
        response = SimpleNamespace(users=[])
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(
                id=99, username="FoundOwner"
            ))
        )
        result = await resolve_current_owner(gift, response, client)
        client.get_entity.assert_awaited_once_with(peer)
        self.assertEqual(profile_url(result), "https://t.me/FoundOwner")

    async def test_lookup_wrong_entity_does_not_replace_owner(self):
        peer = PeerUser(99)
        gift = SimpleNamespace(owner_id=peer)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(
                id=44, username="WrongPerson"
            ))
        )
        result = await resolve_current_owner(gift, SimpleNamespace(), client)
        self.assertEqual(result["label"], "Telegram ID 99")
        self.assertEqual(profile_url(result), "tg://user?id=99")
        self.assertNotIn("WrongPerson", str(result))


if __name__ == "__main__":
    unittest.main()
