"""Generic short-drama workflow graph and side-effect boundaries."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ..core.contracts import (
    AgentRunState,
    AgentRunStatus,
    AgentStage,
    ApprovalDecision,
    AssetReference,
    AuthorizationStatus,
    FindingSeverity,
    ShotPackage,
    StageStatus,
    TaskStatus,
    make_finding,
)
from ..core.errors import AgentBlockedError, AgentPolicyError
from ..core.runtime import AgentGraph, CheckpointStore, InMemoryCheckpointStore
from .compiler import CompiledShot, PromptCompiler, ensure_submit_ready
from .continuity import ContinuityLedger
from .references import bind_references, resolve_authorized_media
from .validator import validate_package


class InternalTaskSubmitter(Protocol):
    def submit(self, *, run: AgentRunState, shot: CompiledShot, idempotency_key: str) -> str: ...


class CloudGatewayTaskSubmitter:
    """Bridge Agent submission to the existing Cloud AI Gateway facade."""

    def __init__(self, submitter: Any, context_factory: Callable[[AgentRunState], Any]):
        self.submitter = submitter
        self.context_factory = context_factory

    def submit(self, *, run: AgentRunState, shot: CompiledShot, idempotency_key: str) -> str:
        from ...platform.ai_gateway_api import submit_ai_task

        result = submit_ai_task(
            self.submitter,
            self.context_factory(run),
            capability=f"video.{shot.generation_mode}",
            idempotency_key=idempotency_key,
            content=shot.gateway_content(),
            project_id=run.project_id,
            media_ids=shot.input_media_ids,
            parameters=shot.gateway_parameters(),
        )
        task_id = result.get("task_id")
        if not task_id:
            raise AgentPolicyError("Cloud AI Gateway 未返回任务 ID", code="GATEWAY_TASK_ID_MISSING")
        return str(task_id)


def assert_internal_submitter(submitter: Any) -> None:
    """Reject provider adapters accidentally injected into Agent nodes."""
    module = str(getattr(type(submitter), "__module__", ""))
    if "video_providers" in module or any(
        hasattr(submitter, name) for name in ("request", "post", "generate_video")
    ):
        raise AgentPolicyError(
            "Agent 提交节点只能调用内部任务工具",
            code="DIRECT_PROVIDER_CALL_FORBIDDEN",
        )


class TaskStatusReader(Protocol):
    def status(self, task_id: str) -> str: ...


@dataclass
class InMemoryTaskSubmitter:
    """Deterministic test adapter; production injects Cloud AI Gateway service."""

    tasks: dict[str, str] = field(default_factory=dict)

    def submit(self, *, run: AgentRunState, shot: CompiledShot, idempotency_key: str) -> str:
        if idempotency_key in self.tasks:
            return self.tasks[idempotency_key]
        task_id = f"agent-task-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        self.tasks[idempotency_key] = task_id
        return task_id


@dataclass
class ShortDramaDependencies:
    compiler: PromptCompiler = field(default_factory=PromptCompiler)
    materials: Mapping[str, Any] = field(default_factory=dict)
    media_resolver: Callable[[str], Any] | None = None
    submitter: InternalTaskSubmitter | None = field(default_factory=InMemoryTaskSubmitter)
    status_reader: TaskStatusReader | Callable[[str], str] | None = None
    require_approval_for_warnings: bool = True
    audio_policy: str = "warning"


WORKFLOW_ORDER = (
    AgentStage.INTAKE,
    AgentStage.STORYBOARD_PLAN,
    AgentStage.CONTINUITY_CHECK,
    AgentStage.ASSET_BINDING,
    AgentStage.PROMPT_COMPILE,
    AgentStage.QUALITY_GATE,
    AgentStage.HUMAN_APPROVAL,
    AgentStage.SUBMIT_TASK,
    AgentStage.MONITOR_TASK,
)


def build_short_drama_graph(dependencies: ShortDramaDependencies | None = None) -> AgentGraph:
    deps = dependencies or ShortDramaDependencies()
    if deps.submitter is not None:
        assert_internal_submitter(deps.submitter)

    def intake(state: AgentRunState) -> AgentRunState:
        if not state.shots:
            raise AgentBlockedError("生产包至少需要一个镜头", code="EMPTY_SHOT_PACKAGE")
        state.audit_events.append({"event": "intake.accepted", "shot_count": len(state.shots)})
        return state

    def storyboard_plan(state: AgentRunState) -> AgentRunState:
        # Existing script/storyboard services can pre-populate ShotPackage. This
        # node is intentionally a typed boundary, not a second script parser.
        state.audit_events.append({"event": "storyboard_plan.accepted", "shot_ids": [s.shot_id for s in state.shots]})
        return state

    def continuity_check(state: AgentRunState) -> AgentRunState:
        findings = ContinuityLedger().check(state.shots)
        state.validation_report = state.validation_report.model_copy(
            update={"findings": [*state.validation_report.findings, *findings]}
        )
        return state

    def asset_binding(state: AgentRunState) -> AgentRunState:
        all_bindings: list[AssetReference] = []
        findings = []
        for shot in state.shots:
            aliases = [reference.alias for reference in shot.references]
            parsed, parsed_findings = bind_references(" ".join(_as_reference_token(alias) for alias in aliases), deps.materials)
            findings.extend(item for item in parsed_findings if item.shot_id is None or item.shot_id == shot.shot_id)
            if deps.media_resolver:
                parsed, permission_findings = resolve_authorized_media(parsed, deps.media_resolver)
                findings.extend(permission_findings)
            all_bindings.extend(parsed)
        state.asset_references = all_bindings
        state.validation_report = state.validation_report.model_copy(
            update={"findings": [*state.validation_report.findings, *findings]}
        )
        return state

    def prompt_compile(state: AgentRunState) -> AgentRunState:
        compiled: list[dict[str, Any]] = []
        findings = []
        for shot in state.shots:
            refs = [reference for reference in state.asset_references if reference.alias in {item.alias for item in shot.references}]
            result = deps.compiler.compile(
                shot,
                profile=state.target_profile,
                bound_references=refs,
                parameters=state.input_summary.get("parameters", {}),
                require_audio=bool(state.input_summary.get("require_audio", False)),
                audio_policy=deps.audio_policy,
            )
            compiled.append(result.model_dump(mode="json"))
            findings.extend(result.findings)
        state.compiled_package = {"profile": state.target_profile, "shots": compiled}
        state.validation_report = state.validation_report.model_copy(
            update={"findings": [*state.validation_report.findings, *findings]}
        )
        return state

    def quality_gate(state: AgentRunState) -> AgentRunState:
        profile = deps.compiler.resolver.resolve(state.target_profile, generation_mode=state.generation_mode)
        report = validate_package(state.shots, references=state.asset_references, profile=profile)
        # Keep compile/continuity findings and deterministic report together.
        report = report.model_copy(update={"findings": [*state.validation_report.findings, *report.findings]})
        state.validation_report = report
        if report.blocking:
            state.status = AgentRunStatus.BLOCKED
            state.stage_status = StageStatus.BLOCKED
        elif report.warnings and deps.require_approval_for_warnings:
            state.status = AgentRunStatus.NEEDS_APPROVAL
            state.approval = state.approval.model_copy(update={"decision": ApprovalDecision.PENDING})
        return state

    def human_approval(state: AgentRunState) -> AgentRunState:
        if state.validation_report.blocking:
            state.status = AgentRunStatus.BLOCKED
            state.stage_status = StageStatus.BLOCKED
            return state
        if state.validation_report.warnings and state.approval.decision is not ApprovalDecision.APPROVED:
            state.status = AgentRunStatus.NEEDS_APPROVAL
            return state
        state.status = AgentRunStatus.RUNNING
        return state

    def submit_task(state: AgentRunState) -> AgentRunState:
        if state.validation_report.blocking:
            raise AgentBlockedError("存在阻断质检项，不能提交视频任务", code="QUALITY_GATE_BLOCKED")
        if state.validation_report.warnings and state.approval.decision is not ApprovalDecision.APPROVED:
            raise AgentBlockedError("警告项需要人工审批", code="APPROVAL_REQUIRED")
        if deps.submitter is None:
            raise AgentPolicyError("未配置内部任务提交工具", code="TASK_SUBMITTER_UNAVAILABLE")
        compiled_shots = (state.compiled_package or {}).get("shots", [])
        for payload in compiled_shots:
            shot = CompiledShot.model_validate(payload)
            ensure_submit_ready(shot)
            key = f"{state.idempotency_key}:{shot.shot_id}"
            task_id = deps.submitter.submit(run=state, shot=shot, idempotency_key=key)
            if task_id not in state.submitted_task_ids:
                state.submitted_task_ids.append(task_id)
        state.task_status = TaskStatus.SUBMITTED
        return state

    def monitor_task(state: AgentRunState) -> AgentRunState:
        if not state.submitted_task_ids:
            state.task_status = TaskStatus.NOT_SUBMITTED
            return state
        statuses = []
        for task_id in state.submitted_task_ids:
            if deps.status_reader is None:
                statuses.append(TaskStatus.SUBMITTED.value)
            elif callable(deps.status_reader):
                statuses.append(str(deps.status_reader(task_id)))
            else:
                statuses.append(str(deps.status_reader.status(task_id)))
        if all(status in {"completed", "succeeded", "provider_succeeded"} for status in statuses):
            state.task_status = TaskStatus.COMPLETED
            state.status = AgentRunStatus.COMPLETED
        elif any(status in {"failed", "cancelled"} for status in statuses):
            state.task_status = TaskStatus.FAILED
            state.status = AgentRunStatus.FAILED
        else:
            state.task_status = TaskStatus.PROCESSING
        return state

    nodes = {
        AgentStage.INTAKE: intake,
        AgentStage.STORYBOARD_PLAN: storyboard_plan,
        AgentStage.CONTINUITY_CHECK: continuity_check,
        AgentStage.ASSET_BINDING: asset_binding,
        AgentStage.PROMPT_COMPILE: prompt_compile,
        AgentStage.QUALITY_GATE: quality_gate,
        AgentStage.HUMAN_APPROVAL: human_approval,
        AgentStage.SUBMIT_TASK: submit_task,
        AgentStage.MONITOR_TASK: monitor_task,
    }
    return AgentGraph(nodes, WORKFLOW_ORDER)


def _as_reference_token(alias: str) -> str:
    if alias.startswith(("character", "scene", "prop")) and ":" in alias:
        prefix, name = alias.split(":", 1)
        kind, slot = prefix.rstrip("0123456789"), prefix[len(prefix.rstrip("0123456789")) :]
        return f"[{kind}{slot}:{name}]"
    if alias.startswith("image:"):
        return f"@图片{alias.split(':', 1)[1]}"
    if alias.startswith("video:"):
        return f"@视频{alias.split(':', 1)[1]}"
    if alias.startswith("audio:"):
        return f"@音频{alias.split(':', 1)[1]}"
    return alias
