import tempfile
import time
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
            "fake-test-token", self.store, "radar_dummy_bot", 100, 1000,
        )

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_merchant_is_explicit(self):
        self.assertEqual(MERCHANT_USERNAME, "nfgtrd22oplbot")
        self.assertEqual(self.merchant.menu()["inline_keyboard"], [
            [{"text": "На 24 часа — 100 ⭐", "callback_data": "buy:1d"}],
            [{"text": "На 30 дней — 1000 ⭐", "callback_data": "buy:30d"}],
        ])

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

    def test_expired_day_can_upgrade_to_30_days(self):
        with patch("commercial.time.time", return_value=1_000):
            p1 = self.store.create_star_order(100, "1d", 100)
            self.assertEqual(self.store.fulfill_star_payment(
                "charge-day", p1, 100, 100
            ), "activated")
            self.assertEqual(self.store.license_info(100)["expires_at"], 87_400)
        with patch("commercial.time.time", return_value=88_000):
            self.assertFalse(self.store.is_subscribed(100))
            p2 = self.store.create_star_order(100, "30d", 1000)
            self.assertEqual(self.store.fulfill_star_payment(
                "charge-month", p2, 100, 1000
            ), "activated")
            self.assertTrue(self.store.is_subscribed(100))
            self.assertEqual(self.store.license_info(100)["plan"], "30d")
            self.assertEqual(self.store.license_info(100)["expires_at"], 88_000 + 30 * 86400)
            self.assertEqual(self.store.stats()["month_active"], 1)
        with patch("commercial.time.time", return_value=88_000 + 30 * 86400):
            self.assertFalse(self.store.is_subscribed(100))

    def test_active_month_renewal_extends_existing_expiry(self):
        with patch("commercial.time.time", return_value=10_000):
            first = self.store.create_star_order(220, "30d", 1000)
            self.assertEqual(self.store.fulfill_star_payment(
                "first-month", first, 220, 1000
            ), "activated")
        with patch("commercial.time.time", return_value=20_000):
            second = self.store.create_star_order(220, "30d", 1000)
            self.assertEqual(self.store.fulfill_star_payment(
                "second-month", second, 220, 1000
            ), "activated")
            self.assertEqual(self.store.license_info(220)["expires_at"],
                             10_000 + 60 * 86400)
            self.assertEqual(self.store.license_info(220)["plan"], "30d")
            self.assertEqual(self.store.fulfill_star_payment(
                "second-month", second, 220, 1000
            ), "already")

    def test_existing_lifetime_is_never_shortened(self):
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO users(chat_id,active,cursor,joined_at,expires_at,plan) "
                "VALUES(101,1,0,0,NULL,'lifetime')"
            )
        for plan, amount in (("1d", 100), ("30d", 1000)):
            order = self.store.create_star_order(101, plan, amount)
            self.assertEqual(self.store.fulfill_star_payment(
                "existing-" + plan, order, 101, amount
            ), "activated")
            self.assertIsNone(self.store.license_info(101)["expires_at"])
            self.assertEqual(self.store.license_info(101)["plan"], "lifetime")

    def test_no_new_lifetime_orders_but_old_issued_invoice_is_honored(self):
        with self.assertRaises(ValueError):
            self.store.create_star_order(200, "lifetime", 3000)
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO star_orders(payload,buyer_id,plan,amount,created_at) "
                "VALUES(?,?,?,?,?)",
                ("preupdate-invoice", 202, "lifetime", 3000, int(time.time())),
            )
        self.assertTrue(self.store.verify_star_order("preupdate-invoice", 202, 3000))
        self.assertEqual(self.store.fulfill_star_payment(
            "legacy-paid", "preupdate-invoice", 202, 3000
        ), "activated")
        self.assertIsNone(self.store.license_info(202)["expires_at"])
        self.assertEqual(self.store.license_info(202)["plan"], "lifetime")

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
        self.assertEqual(args[2]["prices"][0]["amount"], 100)
        self.assertTrue(self.store.verify_star_order(args[2]["payload"], 108, 100))

    async def test_monthly_checkout_sends_1000_xtr_invoice(self):
        self.merchant.call = AsyncMock()
        await self.merchant._handle(None, {"callback_query": {
            "id": "callback-30", "from": {"id": 109},
            "data": "buy:30d",
        }})
        args = self.merchant.call.await_args.args
        self.assertEqual(args[1], "sendInvoice")
        self.assertEqual(args[2]["currency"], "XTR")
        self.assertEqual(args[2]["prices"][0]["amount"], 1000)
        self.assertIn("30 дней", args[2]["title"])
        self.assertTrue(self.store.verify_star_order(args[2]["payload"], 109, 1000))

    async def test_old_lifetime_callback_is_not_available(self):
        self.merchant.call = AsyncMock()
        await self.merchant._handle(None, {"callback_query": {
            "id": "legacy-button", "from": {"id": 110},
            "data": "buy:lifetime",
        }})
        self.merchant.call.assert_awaited_once()
        self.assertEqual(self.merchant.call.await_args.args[1], "answerCallbackQuery")
        self.assertTrue(self.merchant.call.await_args.args[2]["show_alert"])
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM star_orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
