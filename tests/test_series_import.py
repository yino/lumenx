from types import SimpleNamespace

from src.apps.comic_gen.llm import ScriptProcessor


def test_single_episode_import_does_not_call_llm():
    processor = object.__new__(ScriptProcessor)
    processor.llm = SimpleNamespace(is_configured=False)

    episodes = processor.split_into_episodes(
        "第1集：雨夜追凶\n锦衣卫沿着青石长街追击刺客。",
        suggested_episodes=1,
    )

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["episode_number"] == 1
    assert episode["title"] == "雨夜追凶"
    assert episode["summary"] == "第1集：雨夜追凶 锦衣卫沿着青石长街追击刺客。"
    assert episode["start_marker"].startswith("第1集：雨夜追凶")
    assert episode["end_marker"].endswith("锦衣卫沿着青石长街追击刺客。")
    assert episode["estimated_duration"] == ""


def test_single_episode_import_rejects_empty_text():
    processor = object.__new__(ScriptProcessor)
    processor.llm = SimpleNamespace(is_configured=False)

    try:
        processor.split_into_episodes("   \n", suggested_episodes=1)
    except ValueError as exc:
        assert str(exc) == "文件内容为空"
    else:
        raise AssertionError("empty imports must be rejected")
