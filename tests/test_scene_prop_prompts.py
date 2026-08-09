import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.apps.comic_gen.assets import AssetGenerator
from src.apps.comic_gen.models import Prop, Scene, Script
from src.apps.comic_gen.pipeline import ComicGenPipeline


def _script(scene: Scene, prop: Prop) -> Script:
    now = time.time()
    return Script(
        id="project-1",
        title="Prompt test",
        original_text="test",
        scenes=[scene],
        props=[prop],
        created_at=now,
        updated_at=now,
    )


def _pipeline(script: Script) -> ComicGenPipeline:
    pipeline = ComicGenPipeline.__new__(ComicGenPipeline)
    pipeline.scripts = {script.id: script}
    pipeline.series_store = {}
    pipeline.asset_generator = Mock()
    pipeline._save_data = Mock()
    pipeline._save_series_data = Mock()
    return pipeline


def _asset_generator(output_dir: str) -> AssetGenerator:
    generator = AssetGenerator.__new__(AssetGenerator)
    generator.output_dir = output_dir
    generator.model = Mock()
    generator.model.generate.return_value = ("generated.png", None)
    generator._mulerouter_image_model = None
    return generator


class ScenePropPromptTests(unittest.TestCase):
    def setUp(self):
        self.scene = Scene(id="scene-1", name="皇宫大殿", description="庄严宏伟")
        self.prop = Prop(id="prop-1", name="太子血书", description="染血的丝帛")

    def test_pipeline_forwards_workbench_prompts(self):
        script = _script(self.scene, self.prop)
        pipeline = _pipeline(script)

        for asset_type, asset, prompt in (
            ("scene", self.scene, "场景自定义提示词"),
            ("prop", self.prop, "道具自定义提示词"),
        ):
            with self.subTest(asset_type=asset_type):
                pipeline.generate_asset(
                    script.id,
                    asset.id,
                    asset_type,
                    prompt=prompt,
                    apply_style=False,
                )
                generator = getattr(pipeline.asset_generator, f"generate_{asset_type}")
                self.assertEqual(generator.call_args.kwargs["prompt"], prompt)

    def test_scene_generator_uses_chinese_custom_prompt(self):
        with tempfile.TemporaryDirectory() as output_dir:
            generator = _asset_generator(output_dir)
            with patch("src.utils.oss_utils.OSSImageUploader") as uploader:
                uploader.return_value.is_configured = False
                generator.generate_scene(
                    self.scene,
                    prompt="场景自定义提示词",
                    positive_prompt="中国幻想风格",
                )

        generated_prompt = generator.model.generate.call_args.args[0]
        self.assertEqual(generated_prompt, "场景自定义提示词，中国幻想风格")
        self.assertEqual(self.scene.image_asset.variants[0].prompt_used, generated_prompt)

    def test_prop_generator_default_prompt_is_chinese(self):
        with tempfile.TemporaryDirectory() as output_dir:
            generator = _asset_generator(output_dir)
            with patch("src.utils.oss_utils.OSSImageUploader") as uploader:
                uploader.return_value.is_configured = False
                generator.generate_prop(self.prop)

        generated_prompt = generator.model.generate.call_args.args[0]
        self.assertIn("道具设计图：太子血书", generated_prompt)
        self.assertNotIn("Prop Design", generated_prompt)
        self.assertEqual(self.prop.image_asset.variants[0].prompt_used, generated_prompt)


if __name__ == "__main__":
    unittest.main()
