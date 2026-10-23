import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from live_market import (
    normalize_live_event, gift_slug, market_price, verified_public_seller,
    record_sale, estimate_listing, deal_notification, live_nft_notification,
)
from see_live import consume_socket, subscribe_packet


def listing(slug="PlushPepe", num=42, amount=3_500_000_000):
    return {
        "type": "event", "event": "listing",
        "at": "2026-10-24T02:00:00.000Z", "market": "portals",
        "gift": {"slug": slug, "num": num, "title": "Plush Pepe",
                 "model": "Cyberpunk", "backdrop": "Onyx Black",
                 "pattern": "Skull", "owner_name": "Miller"},
        "price": {"amount": amount, "currency": "gram"},
    }


class MarketPureTests(unittest.TestCase):
    def test_documented_listing_gift_and_nano_ton_price(self):
        data = normalize_live_event(listing())
        self.assertEqual(data["slug"], "PlushPepe-42")
        self.assertEqual(data["price"]["ton"], 3.5)
        self.assertEqual(data["price"]["display"], "3.5 TON")
        self.assertIsNone(data["seller"])  # owner_name is not a username

    def test_new_upgrade_gift_resolves(self):
        msg = listing()
        msg["event"] = "new"
        msg.pop("price")
        data = normalize_live_event(msg)
        nft = live_nft_notification(data)
        self.assertEqual(nft["kind"], "new_nft")
        self.assertEqual(nft["slug"], "PlushPepe-42")
        self.assertIsNone(nft["owner_url"])
        self.assertEqual(nft["live_source"], "see.tg WebSocket")

    def test_public_seller_requires_explicit_unhidden_username(self):
        msg = listing()
        msg["seller"] = {"type": "user", "username": "PublicSeller"}
        self.assertEqual(verified_public_seller(msg)["url"],
                         "https://t.me/PublicSeller")
        msg["seller"]["hidden"] = True
        self.assertIsNone(verified_public_seller(msg))

    def test_historical_display_name_never_becomes_chat(self):
        msg = listing()
        msg["gift"]["owner_name"] = "𝒜𝒷𝑒𝓁"
        self.assertIsNone(verified_public_seller(msg))
        msg["gift"]["owner"] = {"type": "user", "displayName": "𝒜𝒷𝑒𝓁"}
        self.assertIsNone(verified_public_seller(msg))

    def test_price_units_are_not_confused(self):
        self.assertEqual(market_price({"amount": 1_500_000, "currency": "usdt"})["display"], "1.5 USDT")
        self.assertEqual(market_price({"amount": 80, "currency": "xtr"})["display"], "80 Stars")
        self.assertIsNone(market_price({"amount": 10, "currency": "fake"}))
        self.assertIsNone(market_price({"amount": True, "currency": "gram"}))

    def test_deal_requires_at_least_three_sales(self):
        candidate = normalize_live_event(listing(amount=2_000_000_000))
        state = {}
        self.assertFalse(estimate_listing(state, candidate)["comparable"])
        for i, amount in enumerate((3_000_000_000, 4_000_000_000)):
            sale = normalize_live_event(listing(num=i + 1, amount=amount) |
                                        {"event": "sale"})
            record_sale(state, sale)
        self.assertFalse(estimate_listing(state, candidate)["comparable"])
        sale = normalize_live_event(listing(num=8, amount=5_000_000_000) |
                                    {"event": "sale"})
        record_sale(state, sale)
        signal = estimate_listing(state, candidate)
        self.assertEqual(signal["median_ton"], 4.0)
        self.assertTrue(signal["potential_bargain"])
        self.assertEqual(signal["sample_size"], 3)
        deal = deal_notification(candidate, signal)
        self.assertEqual(deal["price_display"], "2 TON")
        self.assertEqual(deal["kind"], "market_deal")

    def test_unpriced_new_gift_never_flagged_as_bargain(self):
        msg = listing()
        msg["event"] = "new"
        msg.pop("price")
        gift = normalize_live_event(msg)
        self.assertFalse(estimate_listing({}, gift)["comparable"])
        self.assertIsNone(deal_notification(gift, {"potential_bargain": True}))

    def test_sale_deduplicated(self):
        sale = normalize_live_event(listing(num=3) | {"event": "sale"})
        state = {}
        self.assertTrue(record_sale(state, sale))
        self.assertFalse(record_sale(state, sale))
        self.assertEqual(len(state["live_market_sales"]), 1)

    def test_invalid_payloads_ignored(self):
        self.assertIsNone(normalize_live_event({"type": "event", "event": "new",
                                                "gift": {"slug": "danger/../", "num": 2}}))
        self.assertIsNone(normalize_live_event({"type": "stats"}))
        self.assertIsNone(normalize_live_event({"type": "event", "event": "unknown"}))


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


class LiveWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_auth_requires_no_token_and_receive_event(self):
        results = []
        sock = FakeSocket([listing()])
        async def collect(event):
            results.append(event)
        await consume_socket(sock, collect)
        self.assertEqual(sock.sent[0]["type"], "subscribe")
        self.assertIn("new", sock.sent[0]["events"])
        self.assertEqual(results[0]["slug"], "PlushPepe-42")

    async def test_authenticated_stream_uses_message_not_url_token(self):
        sock = FakeSocket([])
        async def collect(event):
            return None
        await consume_socket(sock, collect, token="SECRET_TOKEN")
        self.assertEqual(sock.sent[0], {"type": "auth", "token": "SECRET_TOKEN"})
        self.assertEqual(sock.sent[1]["type"], "subscribe")

    async def test_bad_json_is_safely_ignored(self):
        collected = []
        sock = FakeSocket([{"type": "stats"}, listing()])
        await consume_socket(sock, lambda ev: captured(collected, ev))
        self.assertEqual(len(collected), 1)


async def captured(items, event):
    items.append(event)


if __name__ == "__main__":
    unittest.main()
