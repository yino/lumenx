"""Application service for creating and resuming short-drama Agent Runs."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from ..core.contracts import (
    AgentRunState,
    AgentRunStatus,
    AgentStage,
    ApprovalDecision,
    ShotPackage,
    StageStatus,
    TaskStatus,
    FindingSeverity,
    make_finding,
)
from ..core.errors import AgentConflictError, AgentNotFoundError
from ..core.runtime import CheckpointStore, InMemoryCheckpointStore
from .graph import ShortDramaDependencies, build_short_drama_graph
from .profiles import ProfileResolver


@dataclass
class InMemoryAgentRunRepository:
    states: dict[str, AgentRunState] = field(default_factory=dict)

    def save(self, state: AgentRunState) -> AgentRunState:
        self.states[state.run_id] = AgentRunState.model_validate(state.model_dump(mode="json"))
        return state

    def get(self, run_id: str) -> AgentRunState | None:
        state = self.states.get(run_id)
        return AgentRunState.model_validate(state.model_dump(mode="json")) if state else None

    def delete(self, run_id: str) -> None:
        self.states.pop(run_id, None)


@dataclass
class ShortDramaAgentService:
    dependencies: ShortDramaDependencies = field(default_factory=ShortDramaDependencies)
    repository: Any = field(default_factory=InMemoryAgentRunRepository)
    checkpoints: CheckpointStore = field(default_factory=InMemoryCheckpointStore)

    def create_run(
        self,
        *,
        project_id: str,
        workspace_id: str,
        owner_user_id: str | None = None,
        shots: list[Any],
        profile: str | None = None,
        generation_mode: str = "r2v",
        idempotency_key: str | None = None,
        skill: str = "short-drama-production",
        skill_alias: str | None = None,
        input_summary: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> AgentRunState:
        chosen_profile = profile or "grok-imagine-video"
        # Resolve before creating a billable run; this is server authoritative.
        self.dependencies.compiler.resolver.resolve(
            chosen_profile,
            generation_mode=generation_mode,
            parameters=(input_summary or {}).get("parameters", {}),
        )
        effective_run_id = run_id or str(uuid.uuid4())
        state = AgentRunState.new(
            run_id=effective_run_id,
            project_id=str(project_id),
            workspace_id=str(workspace_id),
            owner_user_id=str(owner_user_id) if owner_user_id is not None else None,
            target_profile=chosen_profile,
            generation_mode=generation_mode,
            idempotency_key=idempotency_key,
            skill=skill,
            skill_alias=skill_alias,
            input_summary=input_summary,
        )
        state.shots = [item if hasattr(item, "model_dump") else item for item in shots]
        state.shots = [item if isinstance(item, ShotPackage) else ShotPackage.model_validate(item) for item in state.shots]
        existing = self._find_by_idempotency(state.idempotency_key, workspace_id, owner_user_id)
        if existing:
            return existing
        self.repository.save(state)
        self.checkpoints.save(state)
        run_dependencies = self.dependencies
        requested_materials = (input_summary or {}).get("materials")
        if isinstance(requested_materials, Mapping):
            run_dependencies = replace(self.dependencies, materials=dict(requested_materials))
        graph = build_short_drama_graph(run_dependencies)
        try:
            state = graph.invoke(
                state,
                checkpoint=self.checkpoints,
                until=AgentStage.QUALITY_GATE if bool((input_summary or {}).get("preview"))
                else None,
            )
        finally:
            self.repository.save(state)
        if bool((input_summary or {}).get("preview")) and state.status is AgentRunStatus.QUEUED:
            state.status = AgentRunStatus.RUNNING
            self.repository.save(state)
        return state

    def get_run(self, run_id: str, *, workspace_id: str | None = None, owner_user_id: str | None = None) -> AgentRunState:
        try:
            state = self.repository.get(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)
        except TypeError:
            state = self.repository.get(run_id)
        if state is None or (workspace_id is not None and state.workspace_id != str(workspace_id)):
            raise AgentNotFoundError("Agent Run 不存在")
        return state

    def production_package(self, run_id: str, *, workspace_id: str | None = None, owner_user_id: str | None = None) -> dict[str, Any]:
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)
        return {
            "run_id": state.run_id,
            "schema_version": state.schema_version,
            "profile": state.target_profile,
            "shots": copy.deepcopy(state.compiled_package or {}),
            "references": [reference.model_dump(mode="json") for reference in state.asset_references],
            "validation": state.validation_report.model_dump(mode="json"),
            "approval": state.approval.model_dump(mode="json"),
        }

    def approve(self, run_id: str, *, actor_id: str, reason: str | None = None, workspace_id: str | None = None) -> AgentRunState:
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=actor_id)
        if state.status is not AgentRunStatus.NEEDS_APPROVAL:
            raise AgentConflictError("当前 Agent Run 不在待审批状态")
        state.approval = state.approval.model_copy(update={
            "decision": ApprovalDecision.APPROVED,
            "actor_id": str(actor_id),
            "reason": reason,
            "decided_at": datetime.now(UTC),
        })
        state.status = AgentRunStatus.RUNNING
        state.stage_status = StageStatus.COMPLETED
        self.repository.save(state)
        self.checkpoints.save(state)
        return self.resume(run_id, workspace_id=workspace_id, owner_user_id=actor_id)

    def reject(self, run_id: str, *, actor_id: str, reason: str, workspace_id: str | None = None) -> AgentRunState:
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=actor_id)
        if state.status is not AgentRunStatus.NEEDS_APPROVAL:
            raise AgentConflictError("当前 Agent Run 不在待审批状态")
        state.approval = state.approval.model_copy(update={
            "decision": ApprovalDecision.REJECTED,
            "actor_id": str(actor_id),
            "reason": reason,
            "decided_at": datetime.now(UTC),
        })
        state.validation_report = state.validation_report.model_copy(update={
            "findings": [
                *state.validation_report.findings,
                make_finding(FindingSeverity.BLOCKING, "approval_rejected", "人工审批拒绝该生产包", "human-approval"),
            ]
        })
        state.status = AgentRunStatus.BLOCKED
        state.stage_status = StageStatus.BLOCKED
        self.repository.save(state)
        self.checkpoints.save(state)
        return state

    def resume(self, run_id: str, *, workspace_id: str | None = None, owner_user_id: str | None = None) -> AgentRunState:
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)
        checkpoint = self.checkpoints.load(run_id)
        if checkpoint is not None and len(checkpoint.stage_events) >= len(state.stage_events):
            state = checkpoint
        if state.status in {AgentRunStatus.BLOCKED, AgentRunStatus.CANCELLED, AgentRunStatus.COMPLETED}:
            return state
        was_failed = state.status is AgentRunStatus.FAILED
        state.status = AgentRunStatus.RUNNING
        # Monitoring is intentionally repeatable; all earlier stages remain
        # checkpointed while a worker polls the same task IDs.
        state.stage_status = (
            StageStatus.PENDING
            if was_failed or state.current_stage.value == "monitor_task"
            else StageStatus.COMPLETED
        )
        graph = build_short_drama_graph(self._dependencies_for_state(state))
        state = graph.invoke(state, checkpoint=self.checkpoints)
        self.repository.save(state)
        return state

    def cancel(self, run_id: str, *, workspace_id: str | None = None, owner_user_id: str | None = None) -> AgentRunState:
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)
        if state.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}:
            return state
        state.status = AgentRunStatus.CANCELLED
        state.task_status = TaskStatus.CANCELLED
        state.stage_status = StageStatus.SKIPPED
        self.repository.save(state)
        self.checkpoints.save(state)
        return state

    def revise(self, run_id: str, *, shots: list[Any], workspace_id: str | None = None, owner_user_id: str | None = None) -> AgentRunState:
        """Replace the editorial package and restart deterministic gates."""
        state = self.get_run(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)
        state.shots = [item if isinstance(item, ShotPackage) else ShotPackage.model_validate(item) for item in shots]
        state.asset_references = []
        state.validation_report = type(state.validation_report)()
        state.compiled_package = None
        state.approval = type(state.approval)()
        state.submitted_task_ids = []
        state.task_status = TaskStatus.NOT_SUBMITTED
        state.status = AgentRunStatus.RUNNING
        state.current_stage = AgentStage.INTAKE
        state.stage_status = StageStatus.PENDING
        self.repository.save(state)
        self.checkpoints.save(state)
        return self.resume(run_id, workspace_id=workspace_id, owner_user_id=owner_user_id)

    def _find_by_idempotency(self, key: str, workspace_id: str, owner_user_id: str | None = None) -> AgentRunState | None:
        if hasattr(self.repository, "by_idempotency") and owner_user_id:
            return self.repository.by_idempotency(key, workspace_id=str(workspace_id), owner_user_id=str(owner_user_id))
        if not hasattr(self.repository, "states"):
            return None
        for state in self.repository.states.values():
            if state.idempotency_key == key and state.workspace_id == str(workspace_id):
                return AgentRunState.model_validate(state.model_dump(mode="json"))
        return None

    def _dependencies_for_state(self, state: AgentRunState) -> ShortDramaDependencies:
        materials = state.input_summary.get("materials")
        return replace(self.dependencies, materials=dict(materials)) if isinstance(materials, Mapping) else self.dependencies
