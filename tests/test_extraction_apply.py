import time
from unittest.mock import Mock

from src.apps.comic_gen.llm import ScriptProcessor
from src.apps.comic_gen.models import Character, Script
from src.apps.comic_gen.pipeline import ComicGenPipeline


def _script(**overrides) -> Script:
    values = {
        "id": "project-1",
        "title": "零矿纪元",
        "original_text": "旧剧本",
        "style_preset": "cinematic",
        "workflow_mode": "r2v",
        "starred": True,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    values.update(overrides)
    return Script(**values)


def _pipeline(existing: Script) -> ComicGenPipeline:
    pipeline = ComicGenPipeline.__new__(ComicGenPipeline)
    pipeline.scripts = {existing.id: existing}
    pipeline._extraction_cache = {}
    pipeline.script_processor = ScriptProcessor.__new__(ScriptProcessor)
    pipeline._save_data = Mock()
    return pipeline


def test_apply_extraction_rebuilds_submitted_preview_without_llm() -> None:
    pipeline = _pipeline(_script())

    result = pipeline.apply_extraction(
        "project-1",
        "新剧本",
        {
            "characters": [
                {
                    "id": "preview-character",
                    "name": "沈砚",
                    "description": "黑色工装",
                    "visual_weight": 5,
                    # Generated assets from a client payload must not leak
                    # into the newly extracted entity set.
                    "image_url": "file:///old/generated-image.png",
                }
            ],
            "scenes": [
                {
                    "id": "preview-scene",
                    "name": "昆仑轨道港",
                    "description": "冷白色大厅",
                }
            ],
            "props": [
                {
                    "id": "preview-prop",
                    "name": "黑色主螺栓",
                    "description": "磨损的关键零件",
                }
            ],
        },
    )

    assert result.id == "project-1"
    assert result.original_text == "新剧本"
    assert [item.name for item in result.characters] == ["沈砚"]
    assert [item.name for item in result.scenes] == ["昆仑轨道港"]
    assert [item.name for item in result.props] == ["黑色主螺栓"]
    assert result.characters[0].image_url is None
    assert result.style_preset == "cinematic"
    assert result.workflow_mode == "r2v"
    assert result.starred is True
    assert result.created_at == 100.0
    assert result.updated_at > 100.0
    pipeline._save_data.assert_called_once_with()


def test_apply_extraction_uses_matching_fresh_preview_cache() -> None:
    existing = _script()
    cached = _script(
        id="temporary-preview",
        original_text="新剧本",
        characters=[
            Character(
                id="cached-character",
                name="沈砚",
                description="来自预览缓存",
            )
        ],
    )
    pipeline = _pipeline(existing)
    pipeline.script_processor = Mock()
    pipeline._extraction_cache[existing.id] = (time.time(), cached)

    result = pipeline.apply_extraction(
        existing.id,
        "新剧本",
        {"characters": [], "scenes": [], "props": []},
    )

    assert result.characters[0].id == "cached-character"
    pipeline.script_processor.create_script_from_extraction.assert_not_called()
    assert existing.id not in pipeline._extraction_cache

