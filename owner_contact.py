"""Interpret owner fields explicitly returned for a Telegram collectible gift.

No scraping, no guessing of hidden usernames, and no automatic contact with
owners. A TON wallet address is NOT a Telegram chat identity.
"""
from __future__ import annotations

import re
from typing import Any

USER_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def public_username(entity: Any) -> str | None:
    candidates = [getattr(entity, "username", None)]
    for entry in getattr(entity, "usernames", None) or []:
        if getattr(entry, "active", False):
            candidates.append(getattr(entry, "username", None))
    for candidate in candidates:
        if isinstance(candidate, str) and USER_RE.fullmatch(candidate):
            return candidate
    return None


def owner_contact(unique: Any, response: Any) -> dict[str, Any]:
    """Resolve ONLY usernames provided in the getUniqueStarGift result.

    Returns an actionable link for a public username, otherwise documents
    that direct contact was not exposed by Telegram.
    """
    result: dict[str, Any] = {
        "owner_label": "Не раскрыт",
        "owner_url": None,
        "owner_type": "hidden",
        "owner_user_id": None,
        "owner_profile_url": None,
        "offer_min_stars": None,
    }
    minimum = getattr(unique, "offer_min_stars", None)
    if isinstance(minimum, int) and minimum >= 0:
        result["offer_min_stars"] = minimum

    peer = getattr(unique, "owner_id", None)
    kind = type(peer).__name__
    peer_id = None
    entities: list[Any] = []
    if kind == "PeerUser":
        peer_id = getattr(peer, "user_id", None)
        entities = getattr(response, "users", []) or []
        result["owner_type"] = "user"
        if isinstance(peer_id, int) and peer_id > 0:
            # A Telegram Bot API user mention. The ID is in the gift's
            # official public owner_id field; no username guessing.
            # Telegram may refuse to open this for unrelated/private users.
            result["owner_user_id"] = peer_id
            result["owner_profile_url"] = f"tg://user?id={peer_id}"
    elif kind == "PeerChannel":
        peer_id = getattr(peer, "channel_id", None)
        entities = getattr(response, "chats", []) or []
        result["owner_type"] = "channel"
    elif getattr(unique, "owner_address", None):
        result["owner_type"] = "wallet"
        result["owner_label"] = "TON-кошелёк (Telegram-контакт не указан)"
        return result
    elif getattr(unique, "owner_name", None):
        result["owner_label"] = str(unique.owner_name)
        result["owner_type"] = "name"
        return result
    else:
        return result

    for entity in entities:
        if getattr(entity, "id", None) != peer_id:
            continue
        username = public_username(entity)
        if username:
            result["owner_label"] = "@" + username
            result["owner_url"] = "https://t.me/" + username
            result["owner_profile_url"] = result["owner_url"]
            return result
        first_name = getattr(entity, "first_name", None) or getattr(entity, "title", None)
        if first_name:
            result["owner_label"] = str(first_name) + " (без публичного @username)"
        else:
            result["owner_label"] = "Telegram-профиль без публичного @username"
        return result

    result["owner_label"] = "Telegram-профиль без доступного @username"
    return result


async def resolve_owner_contact(unique: Any, response: Any, client: Any) -> dict[str, Any]:
    """Best-effort enrichment if owner_id was actually disclosed by Telegram.

    For PeerUser / PeerChannel only, Telethon may already have a cached full
    entity, even when payments.uniqueStarGift returned UserEmpty. Never try
    to infer user IDs from display names or historical owners.
    """
    result = owner_contact(unique, response)
    if result["owner_url"] or result["owner_type"] not in {"user", "channel"}:
        return result
    peer = getattr(unique, "owner_id", None)
    if peer is None:
        return result
    try:
        entity = await client.get_entity(peer)
    except Exception:
        return result
    if result["owner_type"] == "user":
        if getattr(entity, "id", None) != result["owner_user_id"]:
            return result
    elif getattr(entity, "id", None) != getattr(peer, "channel_id", None):
        return result
    username = public_username(entity)
    if username:
        result["owner_label"] = "@" + username
        result["owner_url"] = "https://t.me/" + username
        result["owner_profile_url"] = result["owner_url"]
    return result


def offer_text(unique: Any, amount_stars: int | None = None) -> str:
    title = str(getattr(unique, "title", "NFT"))
    num = getattr(unique, "num", "?")
    slug = str(getattr(unique, "slug", ""))
    price = (
        f"Готов предложить {amount_stars} ⭐ через официальный оффер Telegram. "
        if amount_stars is not None else
        "Хотел бы обсудить покупку через официальный оффер Telegram. "
    )
    return (
        f"Здравствуйте! Увидел ваш подарок {title} #{num} "
        f"(https://t.me/nft/{slug}). {price}"
        "Вам интересна продажа?"
    )
