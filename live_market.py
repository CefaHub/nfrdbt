"""Normalize public see.tg mint events. No market data or paid API calls."""
from __future__ import annotations
from datetime import datetime, timezone
import re
from typing import Any

GIFT_SLUG = re.compile(r"^[A-Za-z0-9]+-[1-9][0-9]*$")


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


def normalize_live_event(payload: Any) -> dict | None:
    if not isinstance(payload, dict) or payload.get("type") != "event":
        return None
    if payload.get("event") != "new":
        return None
    slug = gift_slug(payload)
    if not slug:
        return None
    gift = payload["gift"]
    return {
        "type": "new",
        "slug": slug,
        "collection": slug.rsplit("-", 1)[0],
        "number": int(slug.rsplit("-", 1)[1]),
        "title": str(gift.get("title") or slug.rsplit("-", 1)[0]),
        "model": str(gift.get("model") or ""),
        "backdrop": str(gift.get("backdrop") or ""),
        "pattern": str(gift.get("pattern") or ""),
        "at": str(payload.get("at") or ""),
    }


def live_nft_notification(event: dict) -> dict:
    return {
        "kind": "new_nft",
        "slug": event["slug"], "number": event["number"],
        "title": event["title"], "model": event["model"],
        "backdrop": event["backdrop"], "pattern": event["pattern"],
        "created_at": event.get("at") or datetime.now(timezone.utc).isoformat(),
        "live_source": "see.tg",
    }
