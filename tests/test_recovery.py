import unittest
from recovery import (
    schedule_recovery, current_recovery, complete_recovery_item,
    fail_recovery_item, pending_count,
)


class TestRecovery(unittest.TestCase):
    def test_plan_bounds_and_no_cursor_change(self):
        data = {"collections": {"PlushPepe": 100, "SnakeBox": 5, "invalid/sl": 800}}
        cursors = dict(data["collections"])
        jobs, count = schedule_recovery(data, lookback=20, max_total=22)
        self.assertEqual((jobs, count), (2, 22))
        self.assertEqual(data["collections"], cursors)
        self.assertEqual(schedule_recovery(data), (0, 22))

    def test_item_retry_progress_and_persistence(self):
        data = {"collections": {"A": 2}}
        schedule_recovery(data, lookback=2)
        self.assertEqual(current_recovery(data), ("A", 1))
        for _ in range(3):
            self.assertFalse(fail_recovery_item(data))
        self.assertTrue(fail_recovery_item(data))
        self.assertEqual(current_recovery(data), ("A", 2))
        complete_recovery_item(data)
        self.assertIsNone(current_recovery(data))
        self.assertEqual(pending_count(data), 0)
        self.assertEqual(data["recovery_skipped"], 1)

    def test_empty_and_existing_jobs(self):
        data = {"collections": {}}
        self.assertEqual(schedule_recovery(data), (0, 0))
        data["collections"]["Hello"] = 17
        self.assertEqual(schedule_recovery(data, lookback=3), (1, 3))
        self.assertEqual(schedule_recovery(data, lookback=100), (0, 3))


if __name__ == "__main__":
    unittest.main()
