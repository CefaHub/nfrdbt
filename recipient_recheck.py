"""Small, bounded retry queue for freshly minted gifts without original details.

Sometimes the collectible counter appears before full gift metadata does.
Retry only newly observed slugs: never replay old collection history.
"""
from __future__ import annotations
import time
from typing import Any

MAX_JOBS = 150
MAX_ATTEMPTS = 4
RETRY_DELAYS = (20, 50, 100, 180)


def schedule_review(data: dict, slug: str, *, now: float | None = None) -> bool:
    if not slug or len(slug) > 100 or "/" in slug:
        return False
    items: dict = data.setdefault("recipient_reviews", {})
    if slug in items:
        return False
    if len(items) >= MAX_JOBS:
        # Keep first-detected discoveries; avoid exceeding persistent state.
        return False
    stamp = time.time() if now is None else now
    items[slug] = {"attempts": 0, "next_at": stamp + RETRY_DELAYS[0]}
    return True


def due_reviews(data: dict, *, now: float | None = None, limit: int = 2) -> list[str]:
    stamp = time.time() if now is None else now
    items = data.get("recipient_reviews") or {}
    return [
        slug for slug, job in items.items()
        if isinstance(job, dict) and job.get("next_at", float("inf")) <= stamp
    ][:max(0, limit)]


def reschedule_review(data: dict, slug: str, *, now: float | None = None) -> bool:
    items = data.get("recipient_reviews") or {}
    if slug not in items:
        return False
    stamp = time.time() if now is None else now
    item = items[slug]
    attempts = int(item.get("attempts", 0)) + 1
    if attempts >= MAX_ATTEMPTS:
        del items[slug]
        return False
    item["attempts"] = attempts
    item["next_at"] = stamp + RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
    return True


def drop_review(data: dict, slug: str) -> bool:
    return (data.get("recipient_reviews") or {}).pop(slug, None) is not None
