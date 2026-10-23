import unittest
from types import SimpleNamespace

from mint_radar import collection_slug, extract_attributes, is_rare_model, make_event


class StarGiftAttributeModel:
    def __init__(self, name, permille=None, rarity=None):
        self.name = name
        self.rarity = rarity or SimpleNamespace(permille=permille)


class StarGiftAttributeBackdrop:
    def __init__(self, name, permille):
        self.name = name
        self.rarity = SimpleNamespace(permille=permille)


class StarGiftAttributePattern:
    def __init__(self, name, permille):
        self.name = name
        self.rarity = SimpleNamespace(permille=permille)


class StarGiftAttributeRarityLegendary:
    pass


class MintTests(unittest.TestCase):
    def test_new_nonrare_nft_event_is_not_marked_rare(self):
        collectible = SimpleNamespace(
            gift_id=123, title="Plush Pepe", slug="PlushPepe-201", num=201,
            attributes=[StarGiftAttributeModel("Common", 150)],
        )
        attrs = extract_attributes(collectible)
        self.assertFalse(is_rare_model(attrs, 1.0))
        event = make_event(collectible, attrs, "2026-10-24T00:00:00+00:00", rare=False)
        self.assertEqual(event["kind"], "new_nft")
        self.assertEqual(event["model_rarity"], "15%")

    def test_rare_nft_keeps_existing_message_type(self):
        collectible = SimpleNamespace(
            gift_id=123, title="Plush Pepe", slug="PlushPepe-202", num=202,
            attributes=[StarGiftAttributeModel("Epic", 5)],
        )
        attrs = extract_attributes(collectible)
        event = make_event(collectible, attrs, "2026-10-24T00:00:00+00:00")
        self.assertEqual(event["kind"], "rare_nft")

    def test_slug(self):
        self.assertEqual(collection_slug("Plush Pepe"), "PlushPepe")

    def test_rare_model_by_permille(self):
        collectible = SimpleNamespace(
            gift_id=123, title="Plush Pepe", slug="PlushPepe-200", num=200,
            attributes=[
                StarGiftAttributeModel("Very Rare", 5),
                StarGiftAttributeBackdrop("Red", 50),
                StarGiftAttributePattern("Dot", 150),
            ],
        )
        attrs = extract_attributes(collectible)
        self.assertEqual(attrs["model"].percentage, 0.5)
        self.assertTrue(is_rare_model(attrs, 1.0))
        event = make_event(collectible, attrs, "2026-10-24T00:00:00+00:00")
        self.assertEqual(event["slug"], "PlushPepe-200")
        self.assertEqual(event["model_rarity"], "0.5%")

    def test_common_model_is_filtered(self):
        attrs = extract_attributes(SimpleNamespace(attributes=[StarGiftAttributeModel("Common", 150)]))
        self.assertFalse(is_rare_model(attrs, 1.0))

    def test_named_tier_legendary(self):
        attrs = extract_attributes(SimpleNamespace(attributes=[
            StarGiftAttributeModel("Awesome", rarity=StarGiftAttributeRarityLegendary())]))
        self.assertTrue(is_rare_model(attrs, 1.0))
        self.assertEqual(attrs["model"].tier, "legendary")


if __name__ == "__main__":
    unittest.main()
