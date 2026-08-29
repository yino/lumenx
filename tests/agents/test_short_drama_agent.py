from __future__ import annotations

import pytest

from src.agents.core import (
    AgentRunState,
    AgentStage,
    InvalidStageTransitionError,
    ShotPackage,
    ShotReference,
    TimelineSegment,
)
from src.agents.short_drama import (
    AgentRouteError,
    PromptCompiler,
    ShortDramaDependencies,
    bind_references,
    normalize_references,
    parse_references,
    validate_shot,
)
from src.agents.short_drama.graph import build_short_drama_graph
from src.agents.short_drama.graph import assert_internal_submitter
from src.agents.short_drama.service import ShortDramaAgentService


def shot(duration: float = 5) -> ShotPackage:
    return ShotPackage(
        shot_id="s1",
        generation_mode="r2v",
        duration=duration,
        subject="角色 A",
        references=[ShotReference(alias="image:1", purpose="角色外观")],
        timeline=[TimelineSegment(start=0, end=duration, action="向前走")],
    )


def test_state_round_trip_redacts_secrets_without_redacting_authorization():
    state = AgentRunState.new(run_id="r1", project_id="p1", workspace_id="w1", target_profile="grok-imagine-video")
    state.input_summary["api_key"] = "secret"
    safe = state.to_safe_dict()
    assert safe["input_summary"]["api_key"] == "[REDACTED]"


def test_unknown_stage_transition_is_rejected():
    state = AgentRunState.new(run_id="r1", project_id="p1", workspace_id="w1", target_profile="grok-imagine-video")
    with pytest.raises(InvalidStageTransitionError):
        state.transition(AgentStage.PROMPT_COMPILE, "running")


def test_reference_normalization_and_binding():
    parsed = parse_references("@图片1 [character1:张成] @图片1")
    assert [item.alias for item in parsed] == ["image:1", "character1:张成"]
    refs, findings = bind_references(parsed, {"image:1": {"media_id": "m1", "authorized": True}, "character1:张成": "m2"})
    assert not findings
    assert [item.media_id for item in refs] == ["m1", "m2"]
    _, conflicts = normalize_references("@图片1 [character1:张成]")
    assert any(item.code == "reference_slot_conflict" for item in conflicts)


def test_invalid_timeline_is_blocking():
    report = validate_shot(shot(5).model_copy(update={"timeline": [TimelineSegment(start=0, end=3, action="x")]}))
    assert report.status == "blocked"
    assert any(item.code == "timeline_duration" for item in report.findings)


def test_graph_pauses_for_unresolved_reference_and_does_not_submit():
    deps = ShortDramaDependencies(materials={})
    service = ShortDramaAgentService(dependencies=deps)
    state = service.create_run(project_id="p1", workspace_id="w1", shots=[shot()])
    assert state.status.value == "blocked"
    assert not state.submitted_task_ids


def test_graph_submission_is_idempotent():
    deps = ShortDramaDependencies(materials={"image:1": {"media_id": "m1", "authorized": True}})
    service = ShortDramaAgentService(dependencies=deps)
    state = service.create_run(project_id="p1", workspace_id="w1", shots=[shot()])
    assert len(state.submitted_task_ids) == 1
    resumed = service.resume(state.run_id, workspace_id="w1")
    assert resumed.submitted_task_ids == state.submitted_task_ids


def test_profile_constraints_and_allowlist():
    from src.agents.short_drama import ProfileResolver

    with pytest.raises(AgentRouteError):
        ProfileResolver().resolve("missing-model", generation_mode="r2v")
    compiled = PromptCompiler().compile(shot(11), profile="grok-imagine-video")
    assert compiled.blocking


def test_warning_requires_approval_before_submission():
    deps = ShortDramaDependencies(materials={"image:1": {"media_id": "m1", "authorized": True}})
    service = ShortDramaAgentService(dependencies=deps)
    state = service.create_run(project_id="p1", workspace_id="w1", shots=[shot().model_copy(update={"timeline": []})])
    assert state.status.value == "needs_approval"
    approved = service.approve(state.run_id, actor_id="u1", workspace_id="w1")
    assert approved.approval.decision.value == "approved"
    assert approved.submitted_task_ids


def test_provider_adapter_cannot_be_used_as_agent_submitter():
    class FakeProvider:
        __module__ = "src.platform.video_providers.fake"

    with pytest.raises(Exception, match="内部任务工具"):
        assert_internal_submitter(FakeProvider())
