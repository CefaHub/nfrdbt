import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from commercial import CommercialStore
from stars_payment import StarsMerchant, MERCHANT_USERNAME


class MerchantPaymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CommercialStore(Path(self.tmp.name) / "commercial.sqlite3")
        self.merchant = StarsMerchant(
            "fake-test-token", self.store, "radar_dummy_bot", 49, 499,
        )

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_merchant_is_explicit(self):
        self.assertEqual(MERCHANT_USERNAME, "nfgtrd22oplbot")
        self.assertEqual(self.merchant.menu()["inline_keyboard"][0][0]["text"],
                         "На 24 часа — 49 ⭐")

    def test_orders_are_buyer_and_amount_bound(self):
        payload = self.store.create_star_order(100, "1d", 49)
        self.assertTrue(self.store.verify_star_order(payload, 100, 49))
        self.assertFalse(self.store.verify_star_order(payload, 101, 49))
        self.assertFalse(self.store.verify_star_order(payload, 100, 50))
        self.assertEqual(self.store.fulfill_star_payment(
            "charge1", payload, 100, 48
        ), "invalid")
        self.assertFalse(self.store.is_subscribed(100))
        self.assertEqual(self.store.fulfill_star_payment(
            "charge1", payload, 100, 49
        ), "activated")
        self.assertFalse(self.store.verify_star_order(payload, 100, 49))
        self.assertEqual(self.store.fulfill_star_payment(
            "charge1", payload, 100, 49
        ), "already")

    def test_renew_expired_day_pass_or_upgrade_to_lifetime(self):
        with patch("commercial.time.time", return_value=1_000):
            p1 = self.store.create_star_order(100, "1d", 49)
            self.assertEqual(self.store.fulfill_star_payment(
                "charge-day", p1, 100, 49
            ), "activated")
            self.assertEqual(self.store.license_info(100)["expires_at"], 87_400)
        with patch("commercial.time.time", return_value=88_000):
            self.assertFalse(self.store.is_subscribed(100))
            p2 = self.store.create_star_order(100, "lifetime", 499)
            self.assertEqual(self.store.fulfill_star_payment(
                "charge-life", p2, 100, 499
            ), "activated")
            self.assertTrue(self.store.is_subscribed(100))
            self.assertIsNone(self.store.license_info(100)["expires_at"])

    def test_existing_lifetime_is_never_shortened(self):
        lifetime = self.store.create_star_order(101, "lifetime", 499)
        self.store.fulfill_star_payment("first-lifetime", lifetime, 101, 499)
        pay = self.store.create_star_order(101, "1d", 49)
        self.assertEqual(self.store.fulfill_star_payment(
            "charge-lifetime", pay, 101, 49
        ), "activated")
        self.assertIsNone(self.store.license_info(101)["expires_at"])

    async def test_wrong_currency_never_grants_access(self):
        payload = self.store.create_star_order(104, "1d", 49)
        self.merchant.call = AsyncMock()
        message = {"message": {
            "from": {"id": 104}, "chat": {"type": "private"},
            "successful_payment": {
                "currency": "USD", "total_amount": 49,
                "invoice_payload": payload,
                "telegram_payment_charge_id": "any",
            },
        }}
        await self.merchant._handle(None, message)
        self.assertFalse(self.store.is_subscribed(104))
        self.merchant.call.assert_not_awaited()

    async def test_successful_payment_grants_access_without_forwarded_invoice(self):
        payload = self.store.create_star_order(105, "1d", 49)
        self.merchant.call = AsyncMock()
        await self.merchant._handle(None, {"message": {
            "from": {"id": 105}, "chat": {"type": "private"},
            "successful_payment": {
                "currency": "XTR", "total_amount": 49,
                "invoice_payload": payload,
                "telegram_payment_charge_id": "charge-id-105",
            },
        }})
        self.assertTrue(self.store.is_subscribed(105))
        self.assertEqual(self.store.license_info(105)["plan"], "1d")
        self.merchant.call.assert_awaited()
        self.assertIn("radar_dummy_bot", self.merchant.call.await_args.args[2]["text"])

    async def test_pre_checkout_rejects_wrong_user_and_amount(self):
        payload = self.store.create_star_order(106, "1d", 49)
        self.merchant.call = AsyncMock()
        await self.merchant._handle(None, {"pre_checkout_query": {
            "id": "query-1", "from": {"id": 107},
            "invoice_payload": payload, "currency": "XTR",
            "total_amount": 49,
        }})
        self.assertFalse(self.merchant.call.await_args.args[2]["ok"])
        await self.merchant._handle(None, {"pre_checkout_query": {
            "id": "query-2", "from": {"id": 106},
            "invoice_payload": payload, "currency": "XTR",
            "total_amount": 49,
        }})
        self.assertTrue(self.merchant.call.await_args.args[2]["ok"])

    async def test_checkout_sends_xtr_invoice_from_merchant_bot(self):
        self.merchant.call = AsyncMock()
        await self.merchant._handle(None, {"callback_query": {
            "id": "callback", "from": {"id": 108},
            "data": "buy:1d",
        }})
        args = self.merchant.call.await_args.args
        self.assertEqual(args[1], "sendInvoice")
        self.assertEqual(args[2]["currency"], "XTR")
        self.assertEqual(args[2]["provider_token"], "")
        self.assertEqual(args[2]["prices"][0]["amount"], 49)
        self.assertTrue(self.store.verify_star_order(args[2]["payload"], 108, 49))


if __name__ == "__main__":
    unittest.main()
