import unittest
from types import SimpleNamespace
from gift_host import hosted_profile

class PeerUser:
    def __init__(self, user_id):
        self.user_id = user_id

class PeerChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id

class HostedProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_host_link_is_not_labeled_as_owner(self):
        unique = SimpleNamespace(host_id=PeerUser(42), owner_address="EQTONwallet")
        response = SimpleNamespace(users=[
            SimpleNamespace(id=42, username="GiftHost42")
        ], chats=[])
        class NoNetwork:
            async def get_entity(self, peer):
                raise AssertionError("Already present in response")
        host = await hosted_profile(unique, response, NoNetwork())
        self.assertEqual(host["url"], "https://t.me/GiftHost42")
        self.assertIn("НЕ подтверждение собственности", host["note"])

    async def test_missing_host_has_no_fake_link(self):
        unique = SimpleNamespace(host_id=None, owner_name="Anonymous")
        host = await hosted_profile(unique, SimpleNamespace(users=[], chats=[]), None)
        self.assertIsNone(host)

    async def test_unresolved_host_is_not_guessed(self):
        unique = SimpleNamespace(host_id=PeerUser(100), owner_name="Hidden")
        class Client:
            async def get_entity(self, peer):
                raise ValueError("Unknown user")
        host = await hosted_profile(unique, SimpleNamespace(users=[], chats=[]), Client())
        self.assertIsNone(host)

    async def test_mismatched_peer_rejected(self):
        unique = SimpleNamespace(host_id=PeerChannel(100))
        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=999, username="WrongHost")
        host = await hosted_profile(unique, SimpleNamespace(users=[], chats=[]), Client())
        self.assertIsNone(host)

if __name__ == "__main__":
    unittest.main()
