import unittest
from scan_pacing import planned_batch_seconds, remaining_pause


class ScanPacingTests(unittest.TestCase):
    def test_minute_sweep_of_121_collections_by_batches_of_five(self):
        self.assertAlmostEqual(planned_batch_seconds(121, 5, 60), 300 / 121)
        elapsed_per_batch = planned_batch_seconds(121, 5, 60)
        self.assertAlmostEqual(elapsed_per_batch * (121 / 5), 60)

    def test_old_behavior_would_take_25_minutes(self):
        # Old bug: sleep 60 seconds after each batch of 5.
        self.assertGreater(60 * ((121 + 4) // 5), 1400)

    def test_processing_time_is_counted_against_the_batch_budget(self):
        target = planned_batch_seconds(121, 5, 60)
        self.assertAlmostEqual(
            remaining_pause(1.0, 121, 5, 60),
            target - 1.0,
        )
        self.assertEqual(remaining_pause(target + 2, 121, 5, 60), 0.0)

    def test_floodwait_overrides_fast_sweep(self):
        self.assertEqual(
            remaining_pause(1.0, 121, 5, 60, floodwait_seconds=60),
            60,
        )

    def test_no_candidates_do_not_spin_cpu(self):
        self.assertEqual(planned_batch_seconds(0, 0, 60), 60.0)


if __name__ == "__main__":
    unittest.main()
