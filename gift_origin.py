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
    elif kind == "PeerChat":
        pid, kind_label = getattr(peer, "chat_id", None), "chat"
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
    """Use only the recipient from Telegram's original gift details."""
    for attr in getattr(unique, "attributes", []) or []:
        if type(attr).__name__ != "StarGiftAttributeOriginalDetails":
            continue
        recipient = _participant(
            getattr(attr, "recipient_id", None), "recipient", response,
        )
        return [recipient] if recipient else []
    return []


async def resolve_original_participants(unique: Any, response: Any, user: Any) -> list[dict]:
    """Resolve the public username of the original recipient when available."""
    results = original_participants(unique, response)
    if not results or results[0].get("public_username"):
        return results
    recipient = results[0]
    attributes = getattr(unique, "attributes", []) or []
    attr = next(
        (x for x in attributes
         if type(x).__name__ == "StarGiftAttributeOriginalDetails"),
        None,
    )
    peer = getattr(attr, "recipient_id", None)
    if peer is None:
        return results
    try:
        entity = await asyncio.wait_for(user.get_entity(peer), timeout=1.0)
    except Exception:
        return results
    if getattr(entity, "id", None) != recipient["id"]:
        return results
    username = public_username(entity)
    if username:
        recipient.update({
            "label": "@" + username,
            "url": "https://t.me/" + username,
            "public_username": username,
        })
    return results
