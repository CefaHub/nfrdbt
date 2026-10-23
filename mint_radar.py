"""Detect recently upgraded collectible gifts by public collection counters.

Reads only public gift metadata (no messaging to NFT owners, no purchases).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


def collection_slug(title: str) -> str:
    # Public gift URLs usually omit spaces/punctuation, e.g. PlushPepe-1.
    # Can be overridden through GIFT_COLLECTIONS if a title maps differently.
    return re.sub(r"[^A-Za-z0-9]", "", title)


@dataclass(frozen=True)
class Attribute:
    name: str
    percentage: float | None
    tier: str | None

    @property
    def printable_rarity(self) -> str:
        if self.percentage is not None:
            return f"{self.percentage:g}%"
        return self.tier or "неизвестна"


def parse_attribute(obj: Any) -> Attribute:
    rarity = getattr(obj, "rarity", None)
    # Modern Telegram API has StarGiftAttributeRarity(permille=int).
    permille = getattr(rarity, "permille", None)
    # Compatibility with older Telethon's direct rarity_permille attribute.
    if permille is None:
        permille = getattr(obj, "rarity_permille", None)
    percent = float(permille) / 10 if permille is not None else None
    tier = None
    if rarity is not None:
        lower = type(rarity).__name__.lower()
        for value in ("legendary", "epic", "rare", "uncommon"):
            if value in lower:
                tier = value
                break
    return Attribute(str(getattr(obj, "name", "?")), percent, tier)


def extract_attributes(unique: Any) -> dict[str, Attribute]:
    result = {}
    for item in getattr(unique, "attributes", []) or []:
        class_name = type(item).__name__.lower()
        for name in ("model", "pattern", "backdrop"):
            if name in class_name:
                result[name] = parse_attribute(item)
                break
    return result


def is_rare_model(attributes: dict[str, Attribute], max_percent: float) -> bool:
    model = attributes.get("model")
    if not model:
        return False
    if model.percentage is not None:
        return model.percentage <= max_percent
    # New named rarity tiers have no exact percentage.
    return model.tier in {"legendary", "epic"}


def make_event(unique: Any, attrs: dict[str, Attribute], detected_at: str, rare: bool = True) -> dict:
    model = attrs.get("model")
    backdrop = attrs.get("backdrop")
    pattern = attrs.get("pattern")
    return {
        "kind": "rare_nft" if rare else "new_nft",
        "gift_id": str(getattr(unique, "gift_id", "")),
        "title": str(getattr(unique, "title", "Collectible")),
        "slug": str(getattr(unique, "slug", "")),
        "number": int(getattr(unique, "num", 0)),
        "model": model.name if model else "?",
        "model_rarity": model.printable_rarity if model else "?",
        "backdrop": backdrop.name if backdrop else "?",
        "backdrop_rarity": backdrop.printable_rarity if backdrop else "?",
        "pattern": pattern.name if pattern else "?",
        "pattern_rarity": pattern.printable_rarity if pattern else "?",
        "created_at": detected_at,
    }
