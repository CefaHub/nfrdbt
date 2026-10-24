"""Read the CURRENT collectible owner, never the original gift recipient.

Telegram's StarGiftUnique.owner_id is the current Telegram owner peer;
owner_address is a TON wallet and owner_name is display text only.
A hosted gift's host_id is NOT the owner. When no verified public profile
exists we omit the profile button rather than link to the wrong person.
"""
from __future__ import annotations

import asyncio
from typing import Any

from gift_origin import public_username


def _peer_identity(peer: Any) -> tuple[str, int, list[str]] | None:
    name = type(peer).__name__
    if name == "PeerUser":
        peer_id = getattr(peer, "user_id", None)
        return ("user", peer_id, ["users"]) if isinstance(peer_id, int) and peer_id > 0 else None
    if name == "PeerChannel":
        peer_id = getattr(peer, "channel_id", None)
        return ("channel", peer_id, ["chats"]) if isinstance(peer_id, int) and peer_id > 0 else None
    if name == "PeerChat":
        peer_id = getattr(peer, "chat_id", None)
        return ("chat", peer_id, ["chats"]) if isinstance(peer_id, int) and peer_id > 0 else None
    return None


def _owner_from_entity(entity: Any, category: str, peer_id: int) -> dict:
    username = public_username(entity)
    if username:
        return {
            "kind": category, "id": peer_id, "label": "@" + username,
            "url": "https://t.me/" + username,
        }
    name = " ".join(str(v) for v in (
        getattr(entity, "first_name", None),
        getattr(entity, "last_name", None),
    ) if v).strip() if category == "user" else str(
        getattr(entity, "title", None) or ""
    ).strip()
    return {
        "kind": category, "id": peer_id,
        "label": name or f"Telegram ID {peer_id}", "url": None,
    }


def current_owner(unique: Any, response: Any) -> dict:
    """Resolve only information Telegram explicitly identifies as ownership."""
    peer = getattr(unique, "owner_id", None)
    identity = _peer_identity(peer)
    if identity is not None:
        category, peer_id, collections = identity
        for collection in collections:
            for entity in getattr(response, collection, []) or []:
                if getattr(entity, "id", None) == peer_id:
                    return _owner_from_entity(entity, category, peer_id)
        return {
            "kind": category, "id": peer_id, "label": f"Telegram ID {peer_id}",
            "url": None,
        }
    wallet = getattr(unique, "owner_address", None)
    if isinstance(wallet, str) and wallet:
        return {
            "kind": "wallet", "id": None,
            "label": "TON " + wallet[:8] + "…" + wallet[-6:]
            if len(wallet) > 20 else "TON " + wallet,
            "url": None,
        }
    owner_name = getattr(unique, "owner_name", None)
    if isinstance(owner_name, str) and owner_name.strip():
        return {
            "kind": "name", "id": None,
            "label": owner_name.strip()[:120], "url": None,
        }
    return {"kind": "unknown", "id": None, "label": "Не указан", "url": None}


async def resolve_current_owner(unique: Any, response: Any, client: Any) -> dict:
    """If public owner username was missing, try a bounded entity lookup.

    The owner ID is always taken from unique.owner_id. Never use the first
    recipient, owner_name or host profile as a substitute for ownership.
    """
    owner = current_owner(unique, response)
    if owner.get("url") or owner["kind"] not in {"user", "channel", "chat"}:
        return owner
    peer = getattr(unique, "owner_id", None)
    try:
        entity = await asyncio.wait_for(client.get_entity(peer), timeout=1.0)
    except Exception:
        return owner
    if getattr(entity, "id", None) != owner["id"]:
        return owner
    return _owner_from_entity(entity, owner["kind"], owner["id"])


def profile_url(owner: Any) -> str | None:
    """Only expose verified public t.me handles to URL buttons."""
    if not isinstance(owner, dict):
        return None
    url = owner.get("url")
    if (owner.get("kind") in {"user", "channel", "chat"}
            and isinstance(url, str) and url.startswith("https://t.me/")
            and url.count("/") == 3):
        handle = url.removeprefix("https://t.me/")
        if len(handle) >= 5 and handle.replace("_", "").isalnum():
            return url
    return None
