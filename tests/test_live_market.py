import json
import unittest

from live_market import gift_slug, normalize_live_event, live_nft_notification
from see_live import HEALTH, consume_socket, subscribe_packet


def mint(number=42):
    return {
        "type": "event", "event": "new",
        "at": "2026-10-24T02:00:00Z",
        "gift": {
            "slug": "PlushPepe", "num": number,
            "title": "Plush Pepe", "model": "Cyberpunk",
            "backdrop": "Onyx Black", "pattern": "Skull",
        },
    }


class MintEventTests(unittest.TestCase):
    def test_slug_and_event(self):
        e = normalize_live_event(mint())
        self.assertEqual(e["slug"], "PlushPepe-42")
        self.assertEqual(e["type"], "new")
        alert = live_nft_notification(e)
        self.assertEqual(alert["kind"], "new_nft")
        self.assertEqual(alert["slug"], "PlushPepe-42")

    def test_ignore_market_events(self):
        for typ in ("listing", "sale", "transfer", "price"):
            self.assertIsNone(normalize_live_event(mint() | {"event": typ}))

    def test_invalid_slug_and_payload(self):
        self.assertIsNone(normalize_live_event(mint() | {
            "gift": {"slug": "../foo", "num": 1},
        }))
        self.assertIsNone(normalize_live_event({"type": "stats"}))

    def test_exact_gift_slug_preserved(self):
        gift = mint()
        gift["gift"] = {"slug": "PlushPepe-200"}
        self.assertEqual(normalize_live_event(gift)["slug"], "PlushPepe-200")


class FakeSocket:
    def __init__(self, packets):
        self.packets = packets
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    def __aiter__(self):
        self._iter = iter(self.packets)
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self._iter))
        except StopIteration:
            raise StopAsyncIteration


async def capture(items, event):
    items.append(event)


class LiveWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_subscribes_to_new_and_stats(self):
        output = []
        sock = FakeSocket([mint()])
        await consume_socket(sock, lambda ev: capture(output, ev))
        self.assertEqual(sock.sent[0], {"type": "subscribe", "events": ["new", "stats"]})
        self.assertEqual(len(output), 1)

    async def test_stats_heartbeat_without_gift_delivery(self):
        items = []
        sock = FakeSocket([{"type": "stats", "events": {"new": 0}}])
        previous = HEALTH["stats_seen"]
        await consume_socket(sock, lambda event: capture(items, event))
        self.assertEqual(items, [])
        self.assertEqual(HEALTH["stats_seen"], previous + 1)
        self.assertIsNotNone(HEALTH["last_stats"])

    async def test_auth_token_only_in_auth_message(self):
        sock = FakeSocket([])
        await consume_socket(sock, lambda ev: capture([], ev), token="secret")
        self.assertEqual(sock.sent[0], {"type": "auth", "token": "secret"})
        self.assertEqual(sock.sent[1]["type"], "subscribe")

    async def test_disabled_new_does_not_break_connection(self):
        sock = FakeSocket([
            {"type": "error", "code": "kind_disabled", "events": ["new"]},
        ])
        await consume_socket(sock, lambda ev: capture([], ev))
        self.assertEqual(HEALTH["disabled_kinds"], ["new"])
        self.assertEqual(HEALTH["status"], "подключено")
        self.assertIsNone(HEALTH["last_error"])

    async def test_enabled_new_resubscribes(self):
        sock = FakeSocket([
            {"type": "kind_disabled", "events": ["new"]},
            {"type": "kind_enabled", "events": ["new"]},
        ])
        await consume_socket(sock, lambda ev: capture([], ev))
        self.assertNotIn("new", HEALTH["disabled_kinds"])
        self.assertEqual(len(sock.sent), 2)

    async def test_unrelated_error_visible(self):
        sock = FakeSocket([{"type": "error", "code": "invalid_request"}])
        await consume_socket(sock, lambda ev: capture([], ev))
        self.assertEqual(HEALTH["last_error"], "see.tg WebSocket: invalid_request")


if __name__ == "__main__":
    unittest.main()
