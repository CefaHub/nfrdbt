import unittest
from recipient_recheck import schedule_review, due_reviews, reschedule_review, drop_review, MAX_ATTEMPTS


class RecipientReviewTests(unittest.TestCase):
    def test_delayed_review_can_recover_discovery(self):
        state = {}
        self.assertTrue(schedule_review(state, "SnowMittens-40508", now=100))
        self.assertFalse(schedule_review(state, "SnowMittens-40508", now=101))
        self.assertEqual(due_reviews(state, now=119), [])
        self.assertEqual(due_reviews(state, now=120), ["SnowMittens-40508"])
        self.assertTrue(reschedule_review(state, "SnowMittens-40508", now=120))
        self.assertEqual(due_reviews(state, now=169), [])
        self.assertEqual(due_reviews(state, now=170), ["SnowMittens-40508"])
        self.assertTrue(drop_review(state, "SnowMittens-40508"))
        self.assertEqual(state["recipient_reviews"], {})

    def test_review_stops_after_fixed_number_of_failures(self):
        state = {}
        schedule_review(state, "SnowMittens-40508", now=0)
        for attempt in range(MAX_ATTEMPTS - 1):
            self.assertTrue(reschedule_review(state, "SnowMittens-40508", now=1000 * (attempt + 1)))
        self.assertFalse(reschedule_review(state, "SnowMittens-40508", now=10000))
        self.assertEqual(due_reviews(state, now=999999), [])

    def test_safe_queue_limit(self):
        state = {}
        for i in range(150):
            self.assertTrue(schedule_review(state, f"Gift-{i+1}", now=0))
        self.assertFalse(schedule_review(state, "Gift-999", now=0))
        self.assertFalse(schedule_review(state, "../bad", now=0))


if __name__ == "__main__":
    unittest.main()
