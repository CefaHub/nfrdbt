"""see.tg live market feed parsing and *conservative* TON price comparisons.

Guest WebSocket events are free. No REST endpoints or paid owner searches.
Live feed is best effort; the existing MTProto collection poller remains active.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
import re
from typing import Any

GIFT_SLUG = re.compile(r"^[A-Za-z0-9]+-[1-9][0-9]*$")
USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")
NOTIFIABLE_TYPES = {"new", "listing", "price"}
TRACKED_TYPES = {"new", "mint", "listing", "price", "sale", "transfer", "opened"}
CURRENCIES = {"gram": ("TON", 1_000_000_000), "usdt": ("USDT", 1_000_000),
              "xtr": ("Stars", 1)}


def gift_slug(payload: dict) -> str | None:
    gift = payload.get("gift")
    if not isinstance(gift, dict):
        return None
    base = gift.get("slug")
    if not isinstance(base, str):
        return None
    if GIFT_SLUG.fullmatch(base):
        return base
    number = gift.get("num")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    candidate = f"{base}-{number}"
    return candidate if GIFT_SLUG.fullmatch(candidate) else None


def collection_of(slug: str) -> str:
    return slug.rsplit("-", 1)[0]


def market_price(raw: Any, normalized_ton: Any = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    amount = raw.get("amount")
    currency = str(raw.get("currency", "")).lower()
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return None
    if currency not in CURRENCIES:
        return None
    label, divisor = CURRENCIES[currency]
    value = amount / divisor
    ton = None
    if currency == "gram":
        ton = value
    elif isinstance(normalized_ton, int) and normalized_ton > 0:
        ton = normalized_ton / 1_000_000_000
    return {"display": f"{value:g} {label}", "ton": ton, "currency": currency}


def verified_public_seller(payload: dict) -> dict | None:
    """Use only explicit publicly disclosed usernames; NEVER owner_name."""
    gift = payload.get("gift")
    if not isinstance(gift, dict):
        return None
    candidate = payload.get("seller") or gift.get("owner")
    if not isinstance(candidate, dict):
        return None
    if candidate.get("hidden") or candidate.get("is_hidden") or candidate.get("isHidden"):
        return None
    kind = str(candidate.get("type", "")).lower()
    if kind not in {"user", "channel"}:
        return None
    username = candidate.get("username")
    if not isinstance(username, str) or not USERNAME.fullmatch(username.removeprefix("@")):
        return None
    value = username.removeprefix("@")
    return {
        "label": "@" + value,
        "url": "https://t.me/" + value,
        "role": "public_" + kind,
    }


def normalize_live_event(payload: Any) -> dict | None:
    if not isinstance(payload, dict) or payload.get("type") != "event":
        return None
    event_type = payload.get("event")
    if event_type not in TRACKED_TYPES:
        return None
    slug = gift_slug(payload)
    if not slug:
        return None
    gift = payload["gift"]
    normalized = {
        "type": event_type,
        "slug": slug,
        "collection": collection_of(slug),
        "number": int(slug.rsplit("-", 1)[1]),
        "title": str(gift.get("title") or collection_of(slug)),
        "model": str(gift.get("model") or "?"),
        "backdrop": str(gift.get("backdrop") or "?"),
        "pattern": str(gift.get("pattern") or "?"),
        "at": str(payload.get("at") or ""),
        "market": str(payload.get("market") or "Telegram"),
        "seller": verified_public_seller(payload),
    }
    price = market_price(payload.get("price"), payload.get("price_ton"))
    if price:
        normalized["price"] = price
    return normalized


def record_sale(state_data: dict, event: dict, max_entries: int = 600) -> bool:
    """Persist only a bounded window of live TON-normalized comparables."""
    if event.get("type") != "sale":
        return False
    price = (event.get("price") or {}).get("ton")
    if not isinstance(price, (float, int)) or price <= 0:
        return False
    key = (event["slug"], event.get("at"))
    sales = state_data.setdefault("live_market_sales", [])
    if any((x.get("slug"), x.get("at")) == key for x in sales[-max_entries:]):
        return False
    sales.append({
        "slug": event["slug"], "collection": event["collection"],
        "model": event.get("model"), "ton": float(price),
        "at": event.get("at"), "market": event.get("market"),
    })
    del sales[:-max_entries]
    return True


def estimate_listing(state_data: dict, event: dict,
                     threshold: float = 0.75, min_comps: int = 3) -> dict:
    """Indicative live-only price signal; NOT a market appraisal or profit claim.

    Same-model sales take priority when at least min_comps exist. Otherwise
    collection sales are used and clearly labeled as a weaker comparison.
    """
    price = (event.get("price") or {}).get("ton")
    if price is None or price <= 0:
        return {"comparable": False, "reason": "Нет цены в TON для сравнения"}
    sales = state_data.get("live_market_sales") or []
    collection = event["collection"]
    similar = [x["ton"] for x in sales if x.get("collection") == collection
               and x.get("model") == event.get("model") and x.get("ton", 0) > 0]
    scope = "та же модель"
    if len(similar) < min_comps:
        similar = [x["ton"] for x in sales if x.get("collection") == collection
                   and x.get("ton", 0) > 0]
        scope = "вся коллекция (модели могут различаться)"
    if len(similar) < min_comps:
        return {"comparable": False, "reason": f"Мало продаж для сравнения ({len(similar)}/{min_comps})"}
    reference = float(median(similar[-40:]))
    if reference <= 0:
        return {"comparable": False, "reason": "Нет надёжных сравнений"}
    ratio = float(price) / reference
    return {
        "comparable": True, "median_ton": round(reference, 5),
        "listing_ton": round(float(price), 5),
        "ratio": round(ratio, 4),
        "discount_pct": round((1 - ratio) * 100, 1),
        "sample_size": min(len(similar), 40), "scope": scope,
        "potential_bargain": ratio <= threshold,
        "note": "Статистика только из наблюдаемых live-продаж; комиссии, ликвидность и особенности NFT не учтены.",
    }


def live_nft_notification(event: dict) -> dict:
    """Shape compatible with existing NFT preview / delivery outbox."""
    seller = event.get("seller") or {}
    # Live feed alone does not prove the owner at send time.
    return {
        "kind": "new_nft", "slug": event["slug"], "number": event["number"],
        "title": event["title"], "model": event["model"],
        "model_rarity": "неизвестна (live)",
        "backdrop": event["backdrop"], "backdrop_rarity": "?",
        "pattern": event["pattern"], "pattern_rarity": "?",
        "owner_label": "Проверить через /owner",
        "owner_type": "hidden", "owner_url": None, "owner_profile_url": None,
        "created_at": event.get("at") or datetime.now(timezone.utc).isoformat(),
        "live_source": "see.tg WebSocket",
        "live_seller_url": seller.get("url"),
        "live_seller_label": seller.get("label"),
    }


def deal_notification(event: dict, assessment: dict) -> dict | None:
    if event.get("type") not in {"listing", "price"} or not assessment.get("potential_bargain"):
        return None
    return {
        "kind": "market_deal", "slug": event["slug"],
        "title": event["title"], "number": event["number"],
        "model": event["model"],
        "price_display": event["price"]["display"],
        "market": event["market"],
        "discount_pct": assessment["discount_pct"],
        "median_ton": assessment["median_ton"],
        "sample_size": assessment["sample_size"],
        "comparison_scope": assessment["scope"],
        "market_note": assessment["note"],
        "seller_url": (event.get("seller") or {}).get("url"),
        "seller_label": (event.get("seller") or {}).get("label"),
        "created_at": event.get("at") or datetime.now(timezone.utc).isoformat(),
        "live_source": "see.tg WebSocket",
    }
