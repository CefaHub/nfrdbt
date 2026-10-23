import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activation import valid_activation_key
from radar import RadarState


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.json"
        self.state = RadarState(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_key_digest_not_plaintext(self):
        from activation import ACTIVATION_KEY_SHA256
        self.assertEqual(len(ACTIVATION_KEY_SHA256), 64)
        self.assertNotIn("NFR-", ACTIVATION_KEY_SHA256)
        self.assertFalse(valid_activation_key("wrong"))

    def test_code_used_once_and_state_survives_restart(self):
        with patch("activation.valid_activation_key", side_effect=lambda code: code == "valid"):
            self.assertTrue(self.state.consume_activation_key("valid", 777))
            self.assertEqual(self.state.owner_id, 777)
            self.assertFalse(self.state.consume_activation_key("valid", 777))
            restored = RadarState(self.path)
            self.assertEqual(restored.owner_id, 777)
            self.assertEqual(restored.activation_status(), "использован")
            self.assertFalse(restored.consume_activation_key("valid", 888))

    def test_whoever_knows_key_cannot_take_existing_owner(self):
        self.state.set_owner(777)
        with patch("activation.valid_activation_key", return_value=True):
            self.assertFalse(self.state.consume_activation_key("valid", 888))
            self.assertEqual(self.state.activation_status(), "не использован")
            self.assertTrue(self.state.consume_activation_key("valid", 777))
            self.assertEqual(RadarState(self.path).owner_id, 777)

    def test_bad_key_does_not_assign_user(self):
        self.assertFalse(self.state.consume_activation_key("invalid", 42))
        self.assertIsNone(self.state.owner_id)


if __name__ == "__main__":
    unittest.main()
