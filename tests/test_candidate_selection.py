import unittest

from candidate_selection import get_candidates


class CandidateSelectionTests(unittest.TestCase):
    def test_known_regular_gifts_without_upgrade_never_enter_nft_scanner(self):
        gifts = {
            "5170145012310081615": {
                "title": "Подарок #5170145012310081615", "upgrade_stars": None,
            },
            "5936013938331222567": {"title": "Plush Pepe", "upgrade_stars": 100},
        }
        found, unresolved = get_candidates(gifts, [], {})
        self.assertEqual(found, [("PlushPepe", 5936013938331222567)])
        self.assertEqual(unresolved, 0)

    def test_known_id_resolves_title_when_telegram_omits_it(self):
        gifts = {
            "5936013938331222567": {
                "title": "Подарок #5936013938331222567", "upgrade_stars": 200,
            }
        }
        found, unresolved = get_candidates(gifts, [], {})
        self.assertEqual(found, [("PlushPepe", 5936013938331222567)])
        self.assertEqual(unresolved, 0)

    def test_unknown_nft_name_is_not_submitted_as_fake_slug(self):
        gifts = {
            "1234567890123": {"title": "Подарок #1234567890123", "upgrade_stars": 25}
        }
        found, unresolved = get_candidates(gifts, [], {})
        self.assertEqual(found, [])
        self.assertEqual(unresolved, 1)

    def test_myth_mask_is_scanned_even_if_catalogue_omits_it(self):
        found, unresolved = get_candidates({}, [], {})
        self.assertIn(("MythMask", None), found)
        self.assertEqual(unresolved, 0)

    def test_manual_slug_has_priority_and_deduplicates_catalogue(self):
        gifts = {"5936013938331222567": {"title": "Plush Pepe", "upgrade_stars": 20}}
        found, unresolved = get_candidates(gifts, ["Plush Pepe", "Plush Pepe"], {})
        self.assertEqual(found, [("PlushPepe", None)])
        self.assertEqual(unresolved, 0)

    def test_validated_resolved_slug_wins_over_guessed_title(self):
        gifts = {"5936013938331222567": {"title": "Wrong Name", "upgrade_stars": 25}}
        found, _ = get_candidates(
            gifts, [], {"5936013938331222567": "PlushPepe"}
        )
        self.assertEqual(found, [("PlushPepe", 5936013938331222567)])


if __name__ == "__main__":
    unittest.main()
