import unittest
from types import SimpleNamespace

from gift_origin import (
    original_participants, resolve_original_participants,
)


class PeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class PeerChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id

class PeerChat:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class StarGiftAttributeOriginalDetails:
    def __init__(self, sender_id, recipient_id):
        self.sender_id = sender_id
        self.recipient_id = recipient_id


class StarGiftAttributeModel:
    def __init__(self, name):
        self.name = name


class OriginalDetailsTests(unittest.TestCase):
    def test_sender_and_first_recipient_public_usernames(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeModel("Common"),
                        StarGiftAttributeOriginalDetails(PeerUser(10), PeerUser(20))],
        )
        response = SimpleNamespace(users=[
            SimpleNamespace(id=10, username="MillerPublic", first_name="Miller"),
            SimpleNamespace(id=20, username="DariiiPublic", first_name="Darii"),
        ], chats=[])
        result = original_participants(gift, response)
        self.assertEqual([p["role"] for p in result], ["recipient"])
        self.assertEqual(result[0]["url"], "https://t.me/DariiiPublic")
        # These are historical gift contacts, not the NFT's current owner.
        self.assertNotIn("owner", result[0])

    def test_numeric_profile_fallback(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(PeerUser(333), PeerUser(444))]
        )
        data = original_participants(gift, SimpleNamespace(users=[], chats=[]))
        self.assertEqual(data[0]["url"], "tg://user?id=444")

    def test_anonymous_sender_does_not_get_guessed(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(None, PeerUser(1234))]
        )
        result = original_participants(gift, SimpleNamespace(users=[], chats=[]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "recipient")

    def test_gift_without_original_details_yields_no_people(self):
        gift = SimpleNamespace(attributes=[StarGiftAttributeModel("Rose")])
        self.assertEqual(original_participants(gift, SimpleNamespace(users=[], chats=[])), [])

    def test_channel_history_never_forges_personal_chat(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(PeerChannel(90), PeerUser(10))]
        )
        data = original_participants(gift, SimpleNamespace(
            users=[], chats=[SimpleNamespace(id=90, username="GiftChannel", title="Gifts")]
        ))
        self.assertEqual(data[0]["url"], "tg://user?id=10")
        self.assertEqual(data[0]["type"], "user")

    def test_recipient_chat_without_public_username_is_preserved(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(None, PeerChat(77))]
        )
        result = original_participants(gift, SimpleNamespace(users=[], chats=[]))
        self.assertEqual(result[0]["role"], "recipient")
        self.assertEqual(result[0]["type"], "chat")
        self.assertIsNone(result[0]["url"])

    def test_malformed_untrusted_peer_does_not_construct_link(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(PeerUser(-1), None)]
        )
        self.assertEqual(original_participants(gift, SimpleNamespace(users=[], chats=[])), [])


class OriginalDetailsAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_username_by_disclosed_peer_id(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(PeerUser(20), PeerUser(30))]
        )
        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=peer.user_id, username=f"UserName{peer.user_id}")
        result = await resolve_original_participants(
            gift, SimpleNamespace(users=[], chats=[]), Client()
        )
        self.assertEqual(result[0]["url"], "https://t.me/UserName30")

    async def test_different_user_id_is_rejected(self):
        gift = SimpleNamespace(
            attributes=[StarGiftAttributeOriginalDetails(None, PeerUser(101))]
        )
        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=999, username="OtherUser")
        result = await resolve_original_participants(
            gift, SimpleNamespace(users=[], chats=[]), Client()
        )
        self.assertEqual(result[0]["url"], "tg://user?id=101")


if __name__ == "__main__":
    unittest.main()
