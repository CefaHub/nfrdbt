"""Manual gift watch: notify only when a real purchase/contact route appears.

Data comes exclusively from the official Telegram unique-gift object.
Never guesses private usernames, scrapes owner history, or makes purchases.
"""
from __future__ import annotations

import re
from typing import Any

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9]+-[1-9][0-9]*$")


def valid_gift_slug(value: str) -> bool:
    return bool(SLUG_PATTERN.fullmatch(value or "")) and len(value) <= 100


def listed_prices(unique: Any) -> list[str]:
    result: list[str] = []
    for price in getattr(unique, "resell_amount", None) or []:
        amount = getattr(price, "amount", None)
        if not isinstance(amount, int) or amount <= 0:
            continue
        kind = type(price).__name__.lower()
        if "ton" in kind:
            result.append(f"{amount / 1_000_000_000:g} TON")
        else:
            nanos = getattr(price, "nanos", 0) or 0
            result.append(f"{amount + nanos / 1_000_000_000:g} ⭐")
    return result


def deal_snapshot(unique: Any, contact: dict) -> dict:
    offer_min = contact.get("offer_min_stars")
    # Official offer is only possible when both offer_min_stars and
    # the owner peer ID are provided by Telegram.
    offer_available = (
        isinstance(offer_min, int)
        and offer_min > 0
        and getattr(unique, "owner_id", None) is not None
    )
    return {
        "owner_url": contact.get("owner_url"),
        "owner_profile_url": contact.get("owner_profile_url"),
        "owner_label": contact.get("owner_label") or "Не раскрыт",
        "owner_type": contact.get("owner_type") or "hidden",
        "offer_available": offer_available,
        "offer_min_stars": offer_min if offer_available else None,
        "resell_prices": listed_prices(unique),
        "slug": str(getattr(unique, "slug", "")),
    }


def deal_opportunities(before: dict, after: dict) -> list[str]:
    """Return only newly actionable conditions, not constant polling chatter."""
    changes: list[str] = []
    if after.get("owner_url") and after.get("owner_url") != before.get("owner_url"):
        changes.append("Появился публичный профиль владельца")
    elif (after.get("owner_profile_url") and
          after.get("owner_profile_url") != before.get("owner_profile_url") and
          not after.get("owner_url")):
        changes.append("Telegram раскрыл ID владельца")
    if after.get("offer_available") and not before.get("offer_available"):
        changes.append("Доступен официальный оффер")
    previous_prices = set(before.get("resell_prices") or [])
    prices = after.get("resell_prices") or []
    if prices and (not previous_prices or set(prices) != previous_prices):
        changes.append("Подарок выставлен на продажу или изменилась цена")
    return changes


def deal_details(snapshot: dict) -> str:
    minimum = snapshot.get("offer_min_stars")
    if snapshot.get("offer_available"):
        offer = f"✅ Официальный оффер от {minimum} ⭐"
    else:
        offer = "❌ Прямой оффер сейчас не подтверждён Telegram API"
    prices = snapshot.get("resell_prices") or []
    listed = " / ".join(prices) if prices else "Не выставлен на официальную перепродажу"
    contact = snapshot.get("owner_url")
    if contact:
        owner = "✅ Публичный чат: " + contact
    elif snapshot.get("owner_profile_url"):
        owner = "⚠️ Профиль по ID (может не открыться): " + snapshot["owner_profile_url"]
    else:
        owner = "🔐 Telegram не раскрывает ссылку на владельца"
    return "\n".join((f"👤 {snapshot.get('owner_label', 'Не раскрыт')}", owner,
                      offer, f"💰 Продажа: {listed}"))
