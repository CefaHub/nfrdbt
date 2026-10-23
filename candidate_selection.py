"""Determine valid collectible collection scan candidates.

Ordinary (non-upgradable) sticker gifts have numeric IDs but no NFT slug.
Do not query GetResaleStarGifts or GetUniqueStarGift for those.
"""
from __future__ import annotations

from gift_catalog import catalogue_name
from mint_radar import collection_slug


def get_candidates(
    gifts: dict[str, dict],
    manual_slugs: list[str],
    resolved: dict[str, str],
) -> tuple[list[tuple[str, int | None]], int]:
    candidates: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    unknown = 0

    for name in manual_slugs:
        slug = collection_slug(name)
        if slug and slug not in seen:
            seen.add(slug)
            candidates.append((slug, None))

    for gift_id, gift in gifts.items():
        # Filtering is essential: normal unlimited gifts have no collectibles.
        if gift.get("upgrade_stars") is None:
            continue
        title = str(gift.get("title") or "")
        if not title or title.startswith("Подарок #"):
            title = catalogue_name(gift_id) or ""
        slug = resolved.get(str(gift_id)) or collection_slug(title)
        if not slug:
            unknown += 1
            continue
        if slug not in seen:
            seen.add(slug)
            candidates.append((slug, int(gift_id)))
    return candidates, unknown
