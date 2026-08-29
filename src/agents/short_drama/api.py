"""Authenticated PC/cloud API for short-drama Agent Runs."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import AgentError
from ...platform.auth.api import AuthApplication
from ...platform.auth.protection import UNSAFE_METHODS
from ...platform.auth.sessions import SessionAuthenticationError, SessionPrincipal
from ...platform.contracts import UserContext, WorkspaceContext
from ...platform.feature_flags import CloudFeatureGate
from .service import ShortDramaAgentService
from .graph import CloudGatewayTaskSubmitter, ShortDramaDependencies


class CreateAgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    profile: str | None = Field(default=None, max_length=120)
    generation_mode: Literal["t2v", "i2v", "r2v"] = "r2v"
    idempotency_key: str | None = Field(default=None, max_length=160)
    skill: str = Field(default="short-drama-production", max_length=120)
    skill_alias: str | None = Field(default=None, max_length=120)
    input_summary: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[dict[str, Any]] = Field(min_length=1, max_length=500)


def install_short_drama_agent_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    service: ShortDramaAgentService | None = None,
    feature_gate: CloudFeatureGate | None = None,
    ai_submitter: Any | None = None,
) -> ShortDramaAgentService:
    if service is None:
        # Cloud runs use the scoped PostgreSQL repository. Tests and local
        # callers can inject the in-memory service explicitly.
        from .repository import PostgresAgentRunRepository

        submitter = None
        if ai_submitter is not None:
            submitter = CloudGatewayTaskSubmitter(
                ai_submitter,
                lambda run: WorkspaceContext(
                    identity=UserContext(
                        user_id=str(run.owner_user_id or ""),
                        session_id=f"agent:{run.run_id}",
                    ),
                    workspace_id=run.workspace_id,
                ),
            )
        active_service = ShortDramaAgentService(
            repository=PostgresAgentRunRepository(auth.database),
            dependencies=ShortDramaDependencies(submitter=submitter) if submitter else ShortDramaDependencies(),
        )
    else:
        active_service = service
    app.state.short_drama_agent_service = active_service
    router = APIRouter(tags=["短剧 Agent"])
    gate = feature_gate

    def require_principal(request: Request) -> SessionPrincipal:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf = request.headers.get("x-csrf-token") if request.method in UNSAFE_METHODS else None
        return auth.sessions.resolve(token, csrf_token=csrf)

    def context(request: Request, principal: SessionPrincipal = Depends(require_principal)) -> tuple[str, str]:
        workspace_id = request.headers.get("x-workspace-id", "").strip()
        if not workspace_id:
            raise HTTPException(status_code=400, detail="请选择有效的工作区")
        return workspace_id, str(principal.user_id)

    def serialized(state) -> dict[str, Any]:
        payload = state.to_safe_dict()
        payload["current_stage"] = state.current_stage.value
        payload["stage_status"] = state.stage_status.value
        payload["status"] = state.status.value
        payload["task_status"] = state.task_status.value
        return payload

    @router.get("/agent/profiles")
    def list_agent_profiles(pair=Depends(context)):
        del pair
        profiles = active_service.dependencies.compiler.resolver._loaded
        allowed_profiles: set[str] | None = None
        if gate is not None:
            feature_state = gate.state()
            allowed_profiles = set(feature_state.short_drama_agent_profiles)
        seen: set[str] = set()
        result = []
        for profile in profiles.values():
            if not profile.enabled or profile.profile_id in seen or (
                allowed_profiles and profile.profile_id not in allowed_profiles
            ):
                continue
            seen.add(profile.profile_id)
            result.append(
                {
                    "profile_id": profile.profile_id,
                    "model_id": profile.model_id,
                    "provider": profile.provider,
                    "display_name": profile.display_name,
                    "modes": sorted(profile.capabilities.modes),
                    "duration": {"min": profile.capabilities.duration_min, "max": profile.capabilities.duration_max},
                    "resolutions": sorted(profile.capabilities.resolutions),
                    "default_resolution": profile.capabilities.default_resolution,
                    "max_reference_images": profile.capabilities.max_reference_images,
                    "supports_audio": profile.capabilities.supports_audio,
                }
            )
        return result

    @router.post("/projects/{project_id}/agent-runs")
    def create_agent_run(project_id: str, body: CreateAgentRunRequest, pair=Depends(context)):
        workspace_id, user_id = pair
        try:
            if gate is not None:
                gate.require_short_drama_agent(body.profile)
            state = active_service.create_run(
                project_id=project_id,
                workspace_id=workspace_id,
                owner_user_id=user_id,
                shots=body.shots,
                profile=body.profile,
                generation_mode=body.generation_mode,
                idempotency_key=body.idempotency_key,
                skill=body.skill,
                skill_alias=body.skill_alias,
                input_summary=body.input_summary,
            )
            state.audit_events.append({"event": "api.create", "actor_id": user_id})
            active_service.repository.save(state)
            return serialized(state)
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/agent-runs/{run_id}")
    def get_agent_run(project_id: str, run_id: str, pair=Depends(context)):
        workspace_id, _ = pair
        try:
            state = active_service.get_run(run_id, workspace_id=workspace_id, owner_user_id=pair[1])
            if state.project_id != str(project_id):
                raise HTTPException(status_code=404, detail="Agent Run 不存在")
            return serialized(state)
        except AgentError as exc:
            raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.get("/projects/{project_id}/agent-runs/{run_id}/production-package")
    def get_production_package(project_id: str, run_id: str, pair=Depends(context)):
        workspace_id, _ = pair
        try:
            state = active_service.get_run(run_id, workspace_id=workspace_id, owner_user_id=pair[1])
            if state.project_id != str(project_id):
                raise HTTPException(status_code=404, detail="Agent Run 不存在")
            return active_service.production_package(run_id, workspace_id=workspace_id, owner_user_id=pair[1])
        except AgentError as exc:
            raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/projects/{project_id}/agent-runs/{run_id}/approve")
    def approve(project_id: str, run_id: str, body: ApprovalRequest, pair=Depends(context)):
        workspace_id, actor_id = pair
        try:
            state = active_service.approve(run_id, actor_id=actor_id, reason=body.reason, workspace_id=workspace_id)
            return serialized(state)
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/projects/{project_id}/agent-runs/{run_id}/reject")
    def reject(project_id: str, run_id: str, body: RejectRequest, pair=Depends(context)):
        workspace_id, actor_id = pair
        try:
            return serialized(active_service.reject(run_id, actor_id=actor_id, reason=body.reason, workspace_id=workspace_id))
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/projects/{project_id}/agent-runs/{run_id}/resume")
    def resume(project_id: str, run_id: str, pair=Depends(context)):
        workspace_id, _ = pair
        try:
            return serialized(active_service.resume(run_id, workspace_id=workspace_id, owner_user_id=pair[1]))
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/projects/{project_id}/agent-runs/{run_id}/revise")
    def revise(project_id: str, run_id: str, body: ReviseRequest, pair=Depends(context)):
        workspace_id, user_id = pair
        try:
            state = active_service.revise(run_id, shots=body.shots, workspace_id=workspace_id, owner_user_id=user_id)
            if state.project_id != str(project_id):
                raise HTTPException(status_code=404, detail="Agent Run 不存在")
            return serialized(state)
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/projects/{project_id}/agent-runs/{run_id}/cancel")
    def cancel(project_id: str, run_id: str, pair=Depends(context)):
        workspace_id, _ = pair
        try:
            return serialized(active_service.cancel(run_id, workspace_id=workspace_id, owner_user_id=pair[1]))
        except AgentError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    app.include_router(router)
    return active_service
