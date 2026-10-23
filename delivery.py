"""Reliable NFT notification outbox.

The scanner writes events to RadarState.pending; this module handles exactly
one persisted event at a time without blocking the scanner. A successful
Telegram send is acknowledged in state.json; failed sends remain on disk.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Telegram rejects some inline buttons / link entities; a plain-text fallback
# can deliver the same NFT link without losing the whole notification.
FORMATTING_ERRORS = (
    "BUTTON_URL_INVALID", "BUTTON_TYPE_INVALID", "BUTTON_DATA_INVALID",
    "ENTITY_BOUNDS_INVALID", "MESSAGE_EMPTY", "MESSAGE_TOO_LONG",
    "REPLY_MARKUP_INVALID", "URL_INVALID",
)


def is_bad_markup(exc: BaseException) -> bool:
    name = type(exc).__name__.upper()
    text = str(exc).upper()
    return any(x in name or x in text for x in FORMATTING_ERRORS)


def fallback_text(item: dict[str, Any]) -> str:
    kind = item.get("kind", "")
    if kind in {"new_nft", "rare_nft"}:
        slug = str(item.get("slug") or "").strip()
        title = str(item.get("title") or "NFT")
        number = item.get("number", "?")
        return (
            "💎 НОВЫЙ NFT (упрощённое уведомление)\n"
            f"{title} #{number}\n"
            f"Модель: {item.get('model', '?')} ({item.get('model_rarity', '?')})\n"
            f"https://t.me/nft/{slug}\n"
            "Полные данные: /last"
        )
    return (
        "🔔 Обновление подарка\n"
        f"{item.get('title', 'Gift')}\n"
        f"Тип: {kind}\n"
        f"Gift ID: {item.get('gift_id', '?')}"
    )


async def dispatch_next(
    state: Any,
    rich_sender: Callable[[dict], Awaitable[None]],
    plain_sender: Callable[[dict], Awaitable[None]],
) -> tuple[str, dict | None]:
    """Attempt delivery of the oldest event, then ACK only on success.

    Returns ('idle', None), ('sent', item) or ('plain', item).
    Raises transport/network/Telegram errors without deleting the event,
    letting a supervising worker apply rate limits and retries.
    """
    if state.owner_id is None or state.data.get("paused"):
        return "idle", None
    pending = state.data.get("pending") or []
    if not pending:
        return "idle", None
    item = pending[0]
    try:
        await rich_sender(item)
        result = "sent"
    except Exception as exc:
        if not is_bad_markup(exc):
            raise
        await plain_sender(item)
        result = "plain"
    state.pop_delivered(item["event_id"])
    return result, item
