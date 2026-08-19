from __future__ import annotations

import uuid

import pytest

from src.apps.comic_gen.models import DialogueStructured, StoryboardFrame
from src.platform.content_repositories import (
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    ScopedDocumentNotFoundError,
)
from src.platform.contracts import MediaWrite
from src.platform.media_storage import CloudMediaStorage
from src.platform.storyboard_service import (
    CloudStoryboardService,
    StoryboardValidationError,
    _build_srt,
    _effective_dialogue,
    _format_srt_timestamp,
)
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script
from tests.test_media_storage import FakePrivateObjectStore


@pytest.fixture
def storyboard_service():
    database = RepositoryDatabase()
    context = _create_scope(database)
    projects = PostgresProjectRepository(database)
    frame = StoryboardFrame(
        id="frame-1",
        scene_id="scene-1",
        action_description="人物走进房间",
        rendered_image_asset=None,
    )
    project = projects.add(context, _script(frames=[frame]))
    other_project = projects.add(context, _script(title="第二集"))
    media = CloudMediaStorage(database, FakePrivateObjectStore())
    yield (
        CloudStoryboardService(database),
        media,
        context,
        project.document.id,
        other_project.document.id,
    )
    database.engine.dispose()


def _store_media(
    media: CloudMediaStorage,
    context,
    project_id: str | None,
    *,
    filename: str = "frame.png",
) -> str:
    return media.store(
        context,
        MediaWrite(
            content=f"content-{uuid.uuid4()}".encode(),
            content_type="image/png",
            filename=filename,
            project_id=project_id,
        ),
    ).media_id


def test_subtitles_prefer_structured_dialogue_and_use_real_timeline() -> None:
    frames = [
        StoryboardFrame(
            id="frame-1",
            scene_id="scene-1",
            dialogue="旧台词",
            speaker="林渊",
            dialogue_structured=DialogueStructured(
                speaker="旁白",
                line="  <b>真龙</b>\n出狱！  ",
            ),
        ),
        StoryboardFrame(id="frame-2", scene_id="scene-1"),
        StoryboardFrame(
            id="frame-3",
            scene_id="scene-2",
            dialogue="警报解除。",
        ),
    ]

    assert _effective_dialogue(frames[0]) == "真龙 出狱！"
    assert _format_srt_timestamp(3661.009) == "01:01:01,009"
    assert _build_srt(frames, [5.038, 2.5, 7.035]) == (
        "1\n"
        "00:00:00,000 --> 00:00:05,038\n"
        "林渊：真龙 出狱！\n\n"
        "2\n"
        "00:00:07,538 --> 00:00:14,573\n"
        "警报解除。\n"
    )


def test_subtitle_builder_rejects_mismatched_durations() -> None:
    frame = StoryboardFrame(id="frame-1", scene_id="scene-1", dialogue="台词")

    with pytest.raises(ValueError, match="数量不一致"):
        _build_srt([frame], [])


def test_storyboard_crud_and_workbench_use_optimistic_versions(
    storyboard_service,
) -> None:
    service, _media, context, project_id, _other_project_id = storyboard_service

    added = service.add_frame(
        context,
        project_id,
        scene_id="scene-2",
        action_description="人物坐下",
        camera_angle="中景",
        insert_at=None,
        expected_version=1,
    )
    added_frame = added.document.frames[-1]
    assert added.version == 2

    updated = service.update_frame(
        context,
        project_id,
        added_frame.id,
        {"dialogue": "开始吧", "duration": 6},
        expected_version=2,
    )
    assert updated.document.frames[-1].dialogue == "开始吧"
    assert updated.version == 3

    workbench = service.update_workbench(
        context,
        project_id,
        added_frame.id,
        {"workbench_tab_mode": "direct_r2v", "workbench_generate_count": 4},
        expected_version=3,
    )
    assert workbench.document.frames[-1].workbench_tab_mode == "direct_r2v"
    assert workbench.document.frames[-1].workbench_generate_count == 4

    copied = service.copy_frame(
        context,
        project_id,
        added_frame.id,
        insert_at=1,
        expected_version=4,
    )
    assert copied.document.frames[1].id != added_frame.id

    reordered = service.reorder_frames(
        context,
        project_id,
        [frame.id for frame in reversed(copied.document.frames)],
        expected_version=5,
    )
    assert reordered.document.frames[0].id == added_frame.id

    locked = service.toggle_frame_lock(
        context,
        project_id,
        added_frame.id,
        expected_version=6,
    )
    assert locked.document.frames[0].locked is True

    deleted = service.delete_frame(
        context,
        project_id,
        added_frame.id,
        expected_version=7,
    )
    assert all(frame.id != added_frame.id for frame in deleted.document.frames)

    with pytest.raises(OptimisticVersionConflictError, match="刷新后重试"):
        service.add_frame(
            context,
            project_id,
            scene_id="scene-3",
            action_description="过期页面",
            camera_angle="近景",
            insert_at=None,
            expected_version=1,
        )


def test_storyboard_workbench_rejects_invalid_client_state(storyboard_service) -> None:
    service, _media, context, project_id, _other_project_id = storyboard_service

    with pytest.raises(StoryboardValidationError, match="工作台模式"):
        service.update_workbench(
            context,
            project_id,
            "frame-1",
            {"workbench_tab_mode": "unknown"},
            expected_version=1,
        )
    with pytest.raises(StoryboardValidationError, match="1 到 6"):
        service.update_workbench(
            context,
            project_id,
            "frame-1",
            {"workbench_generate_count": 7},
            expected_version=1,
        )
    with pytest.raises(StoryboardValidationError, match="首帧选择"):
        service.update_workbench(
            context,
            project_id,
            "frame-1",
            {"t2i_selected_index": -1},
            expected_version=1,
        )


def test_storyboard_media_references_are_scoped_media_ids(storyboard_service) -> None:
    service, media, context, project_id, other_project_id = storyboard_service
    project_media_id = _store_media(media, context, project_id)
    shared_media_id = _store_media(media, context, None, filename="shared.mp3")
    other_media_id = _store_media(media, context, other_project_id)

    rendered = service.attach_frame_media(
        context,
        project_id,
        "frame-1",
        "rendered_image",
        project_media_id,
        expected_version=1,
    )
    frame = rendered.document.frames[0]
    assert frame.rendered_image_asset is not None
    assert frame.rendered_image_url == f"media:{project_media_id}"
    assert frame.rendered_image_asset.variants[0].url == f"media:{project_media_id}"

    video = service.attach_frame_media(
        context,
        project_id,
        "frame-1",
        "video",
        shared_media_id,
        expected_version=2,
    )
    assert video.document.frames[0].video_url == f"media:{shared_media_id}"

    project_output = service.attach_project_media(
        context,
        project_id,
        "merged_video",
        project_media_id,
        expected_version=3,
    )
    assert project_output.document.merged_video_url == f"media:{project_media_id}"

    mixed = service.update_audio_mix(
        context,
        project_id,
        background_music_media_id=shared_media_id,
        clear_background_music=False,
        volume_updates={"dialogue": 90, "bgm": 20, "sfx": 55},
        expected_version=4,
    )
    assert mixed.document.bgm_url == f"media:{shared_media_id}"
    assert mixed.document.mix_settings == {"dialogue": 90, "bgm": 20, "sfx": 55}

    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        service.attach_frame_media(
            context,
            project_id,
            "frame-1",
            "audio",
            other_media_id,
            expected_version=5,
        )
