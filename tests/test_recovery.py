"""Regression tests for /pushall's persisted, bounded NFT rescan."""
from recovery import (
    schedule_recovery, current_recovery, complete_recovery_item,
    fail_recovery_item, pending_count,
)

def test_plan_bounds_and_no_cursor_change():
    data = {"collections": {"PlushPepe": 100, "SnakeBox": 5, "invalid/sl": 800}}
    cursors = dict(data["collections"])
    jobs, count = schedule_recovery(data, lookback=20, max_total=22)
    assert jobs == 2 and count == 22
    assert data["collections"] == cursors
    assert schedule_recovery(data) == (0, 22)

def test_item_retry_progress_and_persistence():
    data = {"collections": {"A": 2}}
    schedule_recovery(data, lookback=2)
    assert current_recovery(data) == ("A", 1)
    for _ in range(3):
        assert fail_recovery_item(data) is False
    assert fail_recovery_item(data) is True
    assert current_recovery(data) == ("A", 2)
    complete_recovery_item(data)
    assert current_recovery(data) is None
    assert pending_count(data) == 0
    assert data["recovery_skipped"] == 1

def test_empty_and_existing_jobs():
    data = {"collections": {}}
    assert schedule_recovery(data) == (0, 0)
    data["collections"]["Hello"] = 17
    assert schedule_recovery(data, lookback=3) == (1, 3)
    assert schedule_recovery(data, lookback=100) == (0, 3)
