"""Backpressure-aware sweep pacing for Telegram NFT collection checks.

One 'scan interval' is the target period for a full pass over candidates,
not the delay between batches of five. Actual duration depends on API latency,
new mints, FloodWait and Telegram restrictions.
"""
from __future__ import annotations


def planned_batch_seconds(total_collections: int, batch_size: int, sweep_seconds: float) -> float:
    if total_collections <= 0 or batch_size <= 0:
        return max(1.0, float(sweep_seconds))
    return max(0.0, float(sweep_seconds) * min(total_collections, batch_size) / total_collections)


def remaining_pause(elapsed_seconds: float, total_collections: int, batch_size: int,
                    sweep_seconds: float, floodwait_seconds: float = 0.0) -> float:
    target = planned_batch_seconds(total_collections, batch_size, sweep_seconds)
    return max(0.0, target - max(0.0, elapsed_seconds), max(0.0, floodwait_seconds))
