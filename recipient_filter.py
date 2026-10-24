"""Filter automatic alerts to gifts with explicit original recipient metadata."""
from __future__ import annotations

NFT_KINDS = {"new_nft", "rare_nft"}

def recipient_from_history(participants: object) -> dict | None:
    if not isinstance(participants, list):
        return None
    for p in participants:
        if not isinstance(p, dict) or p.get("role") != "recipient":
            continue
        # Only Telegram-provided recipient peer IDs, not owner_name or guesses.
        if p.get("type") not in {"user", "channel", "chat"}:
            continue
        user_id = p.get("id")
        url = p.get("url")
        if not isinstance(user_id, int) or user_id <= 0:
            continue
        # Telegram's original-details attribute proves the recipient
        # even if a private channel/chat has no public link.
        if url is None or (isinstance(url, str)
                           and (url.startswith("https://t.me/")
                                or url == f"tg://user?id={user_id}")):
            return p
    return None

def eligible_auto_alert(item: dict) -> bool:
    """Manual delivery probes are exempt; only recipient-bearing NFTs pass."""
    if item.get("kind") == "delivery_test":
        return True
    if item.get("kind") not in NFT_KINDS:
        return False
    return recipient_from_history(item.get("original_participants")) is not None

def filter_pending(state: object) -> int:
    pending = state.data.get("pending") or []
    kept = [event for event in pending if eligible_auto_alert(event)]
    removed = len(pending) - len(kept)
    if removed:
        state.data["pending"] = kept
        state.save()
    return removed
