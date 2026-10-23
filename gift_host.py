"""Public Telegram profile hosting a collectible; host is NOT necessarily owner.

Telegram's starGiftUnique.host_id refers to a profile hosting an on-chain
collectible while owner_address remains the actual wallet owner.
"""
from __future__ import annotations
import asyncio
from typing import Any
from owner_contact import public_username


async def hosted_profile(unique: Any, response: Any, client: Any) -> dict | None:
    peer = getattr(unique, "host_id", None)
    if peer is None:
        return None
    kind = type(peer).__name__
    if kind == "PeerUser":
        peer_id = getattr(peer, "user_id", None)
        entities = getattr(response, "users", []) or []
    elif kind == "PeerChannel":
        peer_id = getattr(peer, "channel_id", None)
        entities = getattr(response, "chats", []) or []
    else:
        return None
    if not isinstance(peer_id, int) or peer_id <= 0:
        return None
    candidate = next((x for x in entities if getattr(x, "id", None) == peer_id), None)
    if candidate is None:
        try:
            candidate = await asyncio.wait_for(client.get_entity(peer), timeout=1.5)
        except Exception:
            pass
    if candidate is None or getattr(candidate, "id", None) != peer_id:
        return None
    username = public_username(candidate)
    if not username:
        return None
    return {
        "label": "@" + username,
        "url": "https://t.me/" + username,
        "role": "host",
        "note": "Профиль, показывающий NFT. НЕ подтверждение собственности.",
    }
