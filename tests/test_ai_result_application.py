from __future__ import annotations

from types import SimpleNamespace

from src.apps.comic_gen.models import StoryboardFrame
from src.platform.ai_result_application import AIResultApplicationService
from src.platform.asset_service import CloudAssetService
from src.platform.content_repositories import PostgresProjectRepository
from src.platform.contracts import MediaWrite
from src.platform.media_storage import CloudMediaStorage
from src.platform.storyboard_service import CloudStoryboardService
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script
from tests.test_media_storage import FakePrivateObjectStore


def test_dialogue_batch_result_is_applied_to_its_frame() -> None:
    database = RepositoryDatabase()
    context = _create_scope(database)
    projects = PostgresProjectRepository(database)
    project = projects.add(
        context,
        _script(
            frames=[
                StoryboardFrame(
                    id="frame-dialogue-1",
                    scene_id="scene-1",
                    dialogue="你终于来了。",
                    speaker="张成",
                )
            ]
        ),
    )
    CloudAssetService(database).create_project_asset(
        context,
        project.document.id,
        "character",
        name="张成",
        description="疲惫的青年",
        voice_id="longcheng_v2",
    )
    content, _stats = CloudStoryboardService(database).build_dialogue_batch_content(
        context,
        project.document.id,
    )
    media_id = CloudMediaStorage(database, FakePrivateObjectStore()).store(
        context,
        MediaWrite(
            content=b"RIFF\x04\x00\x00\x00WAVEdata",
            content_type="audio/wav",
            filename="dialogue.wav",
            project_id=project.document.id,
        ),
    ).media_id
    task = SimpleNamespace(
        project_id=project.document.id,
        request_payload={"content": content},
    )

    try:
        result = AIResultApplicationService(database).apply(
            context,
            task,
            [media_id],
            {"operation": "audio.dialogue.batch", "output_count": 1},
        )

        assert result == {
            "operation": "audio.dialogue.batch",
            "_batch_stats": {
                "generated": 1,
                "skipped": 0,
                "failed": 0,
                "no_voice": 0,
                "total": 1,
            },
        }
        frame = projects.require(context, project.document.id).document.frames[0]
        assert frame.audio_url == f"media:{media_id}"
        assert frame.dialogue_voice_id == "longcheng_v2"
        assert frame.dialogue_text_hash == content["items"][0]["dialogue_text_hash"]
        assert frame.audio_error is None
    finally:
        database.engine.dispose()
