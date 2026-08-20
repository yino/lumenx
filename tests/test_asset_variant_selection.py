import time
import unittest
from unittest.mock import Mock

from src.apps.comic_gen.models import AssetUnit, Character, ImageVariant, Script, Series
from src.apps.comic_gen.pipeline import ComicGenPipeline


def _script(project_id: str, characters=None, series_id=None) -> Script:
    now = time.time()
    return Script(
        id=project_id,
        title="Variant test",
        original_text="test",
        characters=characters or [],
        series_id=series_id,
        created_at=now,
        updated_at=now,
    )


def _character() -> Character:
    first = ImageVariant(id="variant-1", url="assets/characters/one.png")
    second = ImageVariant(id="variant-2", url="assets/characters/two.png")
    return Character(
        id="character-1",
        name="Character",
        description="A test character",
        reference_sheet=AssetUnit(
            selected_image_id=first.id,
            image_variants=[first, second],
        ),
    )


def _pipeline(script: Script, series=None) -> ComicGenPipeline:
    pipeline = ComicGenPipeline.__new__(ComicGenPipeline)
    pipeline.scripts = {script.id: script}
    pipeline.series_store = {series.id: series} if series else {}
    pipeline._save_data = Mock()
    pipeline._save_series_data = Mock()
    return pipeline


class AssetVariantSelectionTests(unittest.TestCase):
    def test_selects_reference_sheet_variant_and_updates_character_cover(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        pipeline.select_asset_variant(
            script.id,
            character.id,
            "character",
            "variant-2",
            "reference_sheet",
        )

        self.assertEqual(character.reference_sheet.selected_image_id, "variant-2")
        self.assertEqual(character.image_url, "assets/characters/two.png")
        pipeline._save_data.assert_called_once()

    def test_selection_falls_back_to_reference_sheet_for_older_clients(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        pipeline.select_asset_variant(
            script.id,
            character.id,
            "character",
            "variant-2",
        )

        self.assertEqual(character.reference_sheet.selected_image_id, "variant-2")
        self.assertEqual(character.image_url, "assets/characters/two.png")

    def test_favorites_reference_sheet_variant(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        pipeline.toggle_variant_favorite(
            script.id,
            character.id,
            "character",
            "variant-2",
            True,
            "reference_sheet",
        )

        self.assertTrue(character.reference_sheet.image_variants[1].is_favorited)
        pipeline._save_data.assert_called_once()

    def test_selects_and_favorites_series_reference_sheet_variant(self):
        character = _character()
        now = time.time()
        series = Series(
            id="series-1",
            title="Series",
            characters=[character],
            created_at=now,
            updated_at=now,
        )
        script = _script("project-1", series_id=series.id)
        pipeline = _pipeline(script, series)

        pipeline.select_asset_variant(
            script.id,
            character.id,
            "character",
            "variant-2",
            "reference_sheet",
        )
        pipeline.toggle_variant_favorite(
            script.id,
            character.id,
            "character",
            "variant-2",
            True,
            "reference_sheet",
        )

        self.assertEqual(character.reference_sheet.selected_image_id, "variant-2")
        self.assertTrue(character.reference_sheet.image_variants[1].is_favorited)
        self.assertEqual(pipeline._save_series_data.call_count, 2)

    def test_missing_reference_sheet_variant_raises_error(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        with self.assertRaisesRegex(ValueError, "Variant missing not found"):
            pipeline.select_asset_variant(
                script.id,
                character.id,
                "character",
                "missing",
                "reference_sheet",
            )

    def test_binds_ark_trusted_material_without_replacing_preview_url(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        pipeline.bind_asset_variant_provider_id(
            script.id,
            character.id,
            "character",
            "variant-1",
            "volcengine_ark",
            "asset://asset-20260820abc123",
        )

        variant = character.reference_sheet.image_variants[0]
        self.assertEqual(variant.url, "assets/characters/one.png")
        self.assertEqual(
            variant.provider_asset_ids["volcengine_ark"],
            "asset-20260820abc123",
        )
        self.assertEqual(
            pipeline._provider_ready_image_references(
                script,
                [variant.url, "https://example.com/scene.png"],
                provider="volcengine_ark",
            ),
            [
                "asset://asset-20260820abc123",
                "https://example.com/scene.png",
            ],
        )

    def test_rejects_invalid_ark_trusted_material_id(self):
        character = _character()
        script = _script("project-1", [character])
        pipeline = _pipeline(script)

        with self.assertRaisesRegex(ValueError, "Expected asset"):
            pipeline.bind_asset_variant_provider_id(
                script.id,
                character.id,
                "character",
                "variant-1",
                "volcengine_ark",
                "https://example.com/not-an-asset-id.png",
            )


if __name__ == "__main__":
    unittest.main()
