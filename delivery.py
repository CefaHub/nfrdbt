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
    "REPLY_MARKUP_INVALID", "URL_INVALID", "MESSAGE_ENTITY_INVALID",
)


def is_bad_markup(exc: BaseException) -> bool:
    # Telethon class names are CamelCase (ButtonUrlInvalidError), while RPC
    # codes use SCREAMING_SNAKE_CASE (BUTTON_URL_INVALID).
    # Normalize both so the fallback works for either representation.
    def normalized(value: str) -> str:
        return "".join(ch for ch in value.upper() if ch.isalnum())

    name = normalized(type(exc).__name__)
    message = normalized(str(exc))
    return any(
        normalized(code) in name or normalized(code) in message
        for code in FORMATTING_ERRORS
    )


def fallback_text(item: dict[str, Any]) -> str:
    if item.get("kind") == "delivery_test":
        return "Проверка доставки\nСообщение отправлено."
    if item.get("kind") in {"new_nft", "rare_nft"}:
        from recipient_filter import recipient_from_history
        person = recipient_from_history(item.get("original_participants"))
        recipient = str(person.get("label") or "Получатель") if person else "Не указан"
        return (
            f"{item.get('title', 'NFT')} #{item.get('number', '?')}\n"
            f"https://t.me/nft/{item.get('slug', '')}\n\n"
            f"Кому подарили: {recipient}\n"
            f"Модель: {item.get('model', '?')}"
        )
    return str(item.get("title") or "Gift Radar")


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
