"""Public original-sender and first-recipient links for collectible gifts.

StarGiftAttributeOriginalDetails holds the original historical participants.
Those people MUST NOT be described as the current NFT owner: the gift may
have been transferred, sold, exported or moved since the original gift.
"""
from __future__ import annotations

import asyncio
from typing import Any
import re

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def public_username(entity: Any) -> str | None:
    candidates = [getattr(entity, "username", None)]
    candidates.extend(
        getattr(x, "username", None)
        for x in (getattr(entity, "usernames", None) or [])
        if getattr(x, "active", False)
    )
    return next(
        (name for name in candidates
         if isinstance(name, str) and _USERNAME_RE.fullmatch(name)),
        None,
    )


def _participant(peer: Any, role: str, response: Any) -> dict | None:
    if peer is None:
        return None
    kind = type(peer).__name__
    if kind == "PeerUser":
        pid, kind_label = getattr(peer, "user_id", None), "user"
        entities = getattr(response, "users", []) or []
    elif kind == "PeerChannel":
        pid, kind_label = getattr(peer, "channel_id", None), "channel"
        entities = getattr(response, "chats", []) or []
    else:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    result = {
        "role": role,
        "type": kind_label,
        "id": pid,
        "label": f"Telegram ID {pid}",
        "url": f"tg://user?id={pid}" if kind_label == "user" else None,
        "public_username": None,
    }
    for entity in entities:
        if getattr(entity, "id", None) != pid:
            continue
        username = public_username(entity)
        if username:
            result["label"] = "@" + username
            result["public_username"] = username
            result["url"] = "https://t.me/" + username
        else:
            display = (getattr(entity, "first_name", None)
                       or getattr(entity, "title", None))
            if display:
                result["label"] = str(display)
        break
    return result


def original_participants(unique: Any, response: Any) -> list[dict]:
    """Parse only the original-details attribute explicitly exposed by API."""
    for attr in getattr(unique, "attributes", []) or []:
        if type(attr).__name__ != "StarGiftAttributeOriginalDetails":
            continue
        sender = _participant(getattr(attr, "sender_id", None), "sender", response)
        receiver = _participant(getattr(attr, "recipient_id", None), "recipient", response)
        # Sender can be intentionally anonymous; never guess an identity.
        return [x for x in (sender, receiver) if x is not None]
    return []


async def resolve_original_participants(unique: Any, response: Any, user: Any) -> list[dict]:
    """Enrich only publicly disclosed peer IDs with accessible Telegram entities."""
    results = original_participants(unique, response)
    attributes = [
        x for x in (getattr(unique, "attributes", None) or [])
        if type(x).__name__ == "StarGiftAttributeOriginalDetails"
    ]
    if not attributes:
        return results
    original = attributes[0]
    peers = {
        "sender": getattr(original, "sender_id", None),
        "recipient": getattr(original, "recipient_id", None),
    }
    for participant in results:
        if participant["public_username"]:
            continue
        peer = peers.get(participant["role"])
        if peer is None:
            continue
        try:
            entity = await asyncio.wait_for(user.get_entity(peer), timeout=1.0)
        except Exception:
            continue
        if getattr(entity, "id", None) != participant["id"]:
            continue
        username = public_username(entity)
        if username:
            participant["label"] = "@" + username
            participant["url"] = "https://t.me/" + username
            participant["public_username"] = username
    return results


