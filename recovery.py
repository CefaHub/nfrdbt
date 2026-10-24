"""Persistent bounded replay of recently scanned NFT collectibles."""
from __future__ import annotations
import re

_VALID_SLUG = re.compile(r"^[A-Za-z0-9]+$")


def pending_count(data: dict) -> int:
    return sum(
        max(0, int(job.get("end", 0)) - int(job.get("next", 1)) + 1)
        for job in data.get("recovery_jobs", []) if isinstance(job, dict)
    )


def schedule_recovery(data: dict, lookback: int = 25,
                      max_total: int = 1500) -> tuple[int, int]:
    """Plan safe lookback without changing production scan cursors.

    Repeated /pushall wakes an existing run rather than discarding progress.
    Returns (number_of_new_jobs, outstanding_gifts).
    """
    jobs = data.setdefault("recovery_jobs", [])
    if jobs:
        return 0, pending_count(data)
    lookback = min(100, max(1, int(lookback)))
    remaining = min(3000, max(1, int(max_total)))
    added = 0
    for slug, issued in sorted((data.get("collections") or {}).items()):
        if not isinstance(slug, str) or not _VALID_SLUG.fullmatch(slug):
            continue
        try:
            end = int(issued)
        except (TypeError, ValueError):
            continue
        if end < 1 or remaining < 1:
            continue
        amount = min(lookback, end, remaining)
        jobs.append({"slug": slug, "next": end - amount + 1,
                     "end": end, "attempts": 0})
        remaining -= amount
        added += 1
    return added, pending_count(data)


def current_recovery(data: dict) -> tuple[str, int] | None:
    jobs = data.get("recovery_jobs", [])
    while jobs and int(jobs[0].get("next", 1)) > int(jobs[0].get("end", 0)):
        jobs.pop(0)
    if not jobs:
        return None
    job = jobs[0]
    return str(job["slug"]), int(job["next"])


def complete_recovery_item(data: dict) -> None:
    jobs = data["recovery_jobs"]
    job = jobs[0]
    job["next"] += 1
    job["attempts"] = 0
    if job["next"] > job["end"]:
        jobs.pop(0)


def fail_recovery_item(data: dict, max_attempts: int = 4) -> bool:
    """Return True if the item was skipped after repeated failures."""
    job = data["recovery_jobs"][0]
    job["attempts"] = int(job.get("attempts", 0)) + 1
    if job["attempts"] < max_attempts:
        return False
    complete_recovery_item(data)
    data["recovery_skipped"] = int(data.get("recovery_skipped", 0)) + 1
    return True
