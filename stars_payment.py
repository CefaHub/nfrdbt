"""Telegram Stars merchant bot for NFT Gift Radar.

The *issuing merchant bot* owns the XTR proceeds. This service only starts
when PAYMENT_BOT_TOKEN belongs to @nfgtrd22oplbot, using Bot API polling.
Buyer uses their usual Telegram account in the merchant bot; the resulting
entitlement is stored for that same user ID in the radar's SQLite database.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from commercial import CommercialStore

LOG = logging.getLogger("gift-radar-stars")
MERCHANT_USERNAME = "nfgtrd22oplbot"


class StarsMerchant:
    def __init__(self, token: str, store: CommercialStore,
                 radar_username: str, price_day: int,
                 price_lifetime: int):
        self.token = token.strip()
        self.store = store
        self.radar_username = radar_username
        self.prices = {"1d": price_day, "lifetime": price_lifetime}
        self.endpoint = f"https://api.telegram.org/bot{self.token}/"
        self.offset = 0

    async def call(self, session: aiohttp.ClientSession,
                   method: str, data: dict[str, Any] | None = None) -> Any:
        # Never log the Bot API URL: it contains a secret bot token.
        async with session.post(
            self.endpoint + method, json=data or {},
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            raw = await resp.json(content_type=None)
            if not isinstance(raw, dict) or not raw.get("ok"):
                code = raw.get("error_code") if isinstance(raw, dict) else resp.status
                raise RuntimeError(f"Merchant Bot API {method} failed: HTTP {code}")
            return raw.get("result")

    async def send_message(self, session: aiohttp.ClientSession, uid: int,
                           text: str, markup: dict | None = None) -> None:
        request: dict[str, Any] = {"chat_id": uid, "text": text,
                                    "disable_web_page_preview": True}
        if markup:
            request["reply_markup"] = markup
        await self.call(session, "sendMessage", request)

    def menu(self) -> dict[str, Any]:
        buttons = []
        if self.prices["1d"] > 0:
            buttons.append([{"text": f"На 24 часа — {self.prices['1d']} ⭐",
                             "callback_data": "buy:1d"}])
        if self.prices["lifetime"] > 0:
            buttons.append([{"text": f"Бессрочно — {self.prices['lifetime']} ⭐",
                             "callback_data": "buy:lifetime"}])
        return {"inline_keyboard": buttons}

    async def _handle(self, session: aiohttp.ClientSession,
                      update: dict[str, Any]) -> None:
        check = update.get("pre_checkout_query")
        if check:
            uid = int(check["from"]["id"])
            valid = (
                check.get("currency") == "XTR"
                and self.store.verify_star_order(
                    str(check.get("invoice_payload") or ""),
                    uid, int(check.get("total_amount") or 0)
                )
            )
            await self.call(session, "answerPreCheckoutQuery", {
                "pre_checkout_query_id": check["id"], "ok": valid,
                **({} if valid else {"error_message":
                   "Счёт недействителен или устарел. Создайте новый через /buy."}),
            })
            return

        message = update.get("message")
        if message:
            uid = int(message.get("from", {}).get("id", 0))
            if uid <= 0 or message.get("chat", {}).get("type") != "private":
                return
            payment = message.get("successful_payment")
            if payment:
                if payment.get("currency") != "XTR":
                    return
                result = self.store.fulfill_star_payment(
                    str(payment.get("telegram_payment_charge_id") or ""),
                    str(payment.get("invoice_payload") or ""),
                    uid, int(payment.get("total_amount") or 0),
                )
                if result == "activated":
                    await self.send_message(
                        session, uid,
                        "Оплата получена! Доступ к NFT Gift Radar подключён.\n"
                        "Открой радар и нажми Start, чтобы получать уведомления:\n"
                        f"https://t.me/{self.radar_username}\n\n"
                        "В радаре используй /status, чтобы проверить доступ."
                    )
                elif result == "invalid":
                    LOG.error("Payment received but invoice failed verification; "
                              "manual reconciliation required (no user IDs logged)")
                    await self.send_message(
                        session, uid, "Платёж получен, но автоматическая активация "
                        "не завершилась. Обратись в поддержку через Telegram "
                        "или службу поддержки площадки с квитанцией оплаты."
                    )
                return
            text = (message.get("text") or "").strip()
            if text.startswith(("/start", "/buy", "/help")):
                if not self.menu()["inline_keyboard"]:
                    await self.send_message(session, uid,
                                            "Оплата временно не настроена.")
                    return
                await self.send_message(
                    session, uid,
                    "NFT Gift Radar — оплата через Telegram Stars ⭐\n"
                    "Выбери тариф. Доступ активируется только после "
                    "подтверждения платежа Telegram.\n"
                    "Бот с оплатой и радар используют один Telegram ID.",
                    self.menu(),
                )
            return

        callback = update.get("callback_query")
        if callback:
            uid = int(callback["from"]["id"])
            plan = (callback.get("data") or "").removeprefix("buy:")
            valid = (callback.get("data") or "").startswith("buy:") and plan in self.prices
            price = self.prices.get(plan, 0)
            if not valid or price <= 0:
                await self.call(session, "answerCallbackQuery", {
                    "callback_query_id": callback["id"],
                    "text": "Тариф сейчас недоступен.",
                    "show_alert": True,
                })
                return
            if self.store.license_info(uid) and self.store.license_info(uid)["plan"] == "lifetime":
                await self.call(session, "answerCallbackQuery", {
                    "callback_query_id": callback["id"],
                    "text": "У тебя уже есть бессрочный доступ. Повторная покупка не нужна.",
                    "show_alert": True,
                })
                return
            await self.call(session, "answerCallbackQuery",
                            {"callback_query_id": callback["id"]})
            payload = self.store.create_star_order(uid, plan, price)
            title = "NFT Radar — 24 часа" if plan == "1d" else "NFT Radar — бессрочно"
            description = (
                "Доступ к мониторингу NFT-подарков на 24 часа с момента оплаты."
                if plan == "1d" else
                "Бессрочный доступ к работающему NFT Gift Radar."
            )
            await self.call(session, "sendInvoice", {
                "chat_id": uid, "title": title,
                "description": description,
                "payload": payload,
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": title, "amount": price}],
            })

    async def run(self) -> None:
        if not self.token:
            return
        if not self.radar_username:
            LOG.error("Payment disabled: radar bot has no username")
            return
        async with aiohttp.ClientSession() as session:
            try:
                me = await self.call(session, "getMe")
                username = (me.get("username") or "").lower()
                if username != MERCHANT_USERNAME:
                    LOG.error("Payment disabled: token is not for expected "
                              "merchant @%s", MERCHANT_USERNAME)
                    return
                if username == self.radar_username.lower():
                    LOG.error("Payment disabled: payment bot must be separate "
                              "from the radar Telethon bot")
                    return
                webhook = await self.call(session, "getWebhookInfo")
                if webhook.get("url"):
                    LOG.error("Payment disabled: merchant bot has webhook; "
                              "remove it manually before polling on Bothost")
                    return
            except Exception as exc:
                LOG.error("Merchant initialization failed: %s", type(exc).__name__)
                return
            LOG.info("Telegram Stars merchant initialized for @%s", username)
            backoff = 2
            while True:
                try:
                    updates = await self.call(session, "getUpdates", {
                        "offset": self.offset,
                        "timeout": 20,
                        "limit": 20,
                        "allowed_updates": [
                            "message", "callback_query", "pre_checkout_query"
                        ],
                    })
                    for update in updates:
                        await self._handle(session, update)
                        self.offset = int(update["update_id"]) + 1
                    backoff = 2
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.error("Merchant polling error: %s", type(exc).__name__)
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
