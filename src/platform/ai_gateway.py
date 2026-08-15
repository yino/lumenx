from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .ai_gateway_contract import (
    AIGatewayRequest,
    AIGatewayRequestContract,
)
from .ai_task_state import (
    AITaskStateConflictError,
    AITaskStateService,
    TERMINAL_TASK_STATUSES,
)
from .contracts import ModelRouteSnapshot, TaskDispatcher, WorkspaceContext
from .db_models import AssetRecord, MediaObjectRecord, ProjectRecord, SeriesRecord
from .metering import MeteringValidationError, maximum_metering_tokens
from .identifiers import parse_database_id
from .model_routing import CapabilityRoutePlan, thaw_snapshot_value
from .ticket_reservation import ReservedTask, TicketReservationService


class AIGatewayError(RuntimeError):
    pass


class AIGatewayResourceNotFoundError(AIGatewayError):
    pass


class AIGatewayConfigurationError(AIGatewayError):
    pass


class AIGatewayDispatchError(AIGatewayError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__("AI 任务已保存，等待队列恢复后继续执行")


class RoutePlanProvider(Protocol):
    def build_plan(
        self,
        capability: str,
        requested_parameters: Mapping[str, Any],
    ) -> CapabilityRoutePlan: ...


@dataclass(frozen=True, slots=True)
class SubmittedAITask:
    task_id: str
    attempt_id: str | None
    status: str
    capability: str
    quoted_microtickets: int
    tokens_per_ticket: int
    model_display_name: str
    provider_model_id: str
    reused: bool
    dispatched: bool


def _route_payload(route: ModelRouteSnapshot) -> dict[str, Any]:
    return {
        "config_version_id": route.config_version_id,
        "route_id": route.route_id,
        "capability": route.capability,
        "provider": route.provider,
        "provider_model_id": route.provider_model_id,
        "display_name": route.display_name,
        "parameters": thaw_snapshot_value(route.parameters),
        "metering_formula": thaw_snapshot_value(route.metering_formula),
        "fallback_policy": thaw_snapshot_value(route.fallback_policy),
        "secret_ref": route.secret_ref,
    }


class GatewayResourceAuthorizer:
    def __init__(self, context: WorkspaceContext, request: AIGatewayRequest) -> None:
        self.context = context
        self.request = request
        self.user_id = parse_database_id(context.identity.user_id, field="用户 ID")
        self.workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")

    @staticmethod
    def _payload_contains_id(value: Any, identifier: int) -> bool:
        expected = str(identifier)
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) in {"id", "frame_id"} and str(item) == expected:
                    return True
                if GatewayResourceAuthorizer._payload_contains_id(item, identifier):
                    return True
        elif isinstance(value, list):
            return any(
                GatewayResourceAuthorizer._payload_contains_id(item, identifier)
                for item in value
            )
        return False

    def _project(self, session: Session) -> ProjectRecord | None:
        if self.request.project_id is None:
            return None
        project = session.scalar(
            select(ProjectRecord).where(
                ProjectRecord.id == self.request.project_id,
                ProjectRecord.user_id == self.user_id,
                ProjectRecord.workspace_id == self.workspace_id,
                ProjectRecord.deleted_at.is_(None),
            )
        )
        if project is None:
            raise AIGatewayResourceNotFoundError("AI 请求资源不存在")
        return project

    def _authorize_media(self, session: Session) -> None:
        if not self.request.media_ids:
            return
        conditions = [
            MediaObjectRecord.id.in_(self.request.media_ids),
            MediaObjectRecord.user_id == self.user_id,
            MediaObjectRecord.workspace_id == self.workspace_id,
            MediaObjectRecord.lifecycle_state == "active",
            MediaObjectRecord.deleted_at.is_(None),
        ]
        if self.request.project_id is not None:
            conditions.append(
                or_(
                    MediaObjectRecord.project_id.is_(None),
                    MediaObjectRecord.project_id == self.request.project_id,
                )
            )
        found = set(session.scalars(select(MediaObjectRecord.id).where(*conditions)))
        if found != set(self.request.media_ids):
            raise AIGatewayResourceNotFoundError("AI 请求资源不存在")

    def _asset_records(
        self,
        session: Session,
        identifiers: list[int],
    ) -> dict[int, AssetRecord]:
        if not identifiers:
            return {}
        ownership = or_(
            AssetRecord.scope == "system",
            and_(
                AssetRecord.user_id == self.user_id,
                AssetRecord.workspace_id == self.workspace_id,
            ),
        )
        records = list(
            session.scalars(
                select(AssetRecord).where(
                    AssetRecord.id.in_(identifiers),
                    AssetRecord.deleted_at.is_(None),
                    ownership,
                )
            )
        )
        by_id = {record.id: record for record in records}
        if set(by_id) != set(identifiers):
            raise AIGatewayResourceNotFoundError("AI 请求资源不存在")
        return by_id

    def _authorize_resources(
        self,
        session: Session,
        project: ProjectRecord | None,
    ) -> None:
        resource_ids = self.request.resource_ids
        series_ids = resource_ids.get("series", [])
        if series_ids:
            found = set(
                session.scalars(
                    select(SeriesRecord.id).where(
                        SeriesRecord.id.in_(series_ids),
                        SeriesRecord.user_id == self.user_id,
                        SeriesRecord.workspace_id == self.workspace_id,
                        SeriesRecord.deleted_at.is_(None),
                    )
                )
            )
            if found != set(series_ids):
                raise AIGatewayResourceNotFoundError("AI 请求资源不存在")

        asset_ids = resource_ids.get("asset", [])
        self._asset_records(session, asset_ids)

        voice_ids = resource_ids.get("voice", [])
        voices = self._asset_records(session, voice_ids)
        if any(record.asset_type != "voice" for record in voices.values()):
            raise AIGatewayResourceNotFoundError("AI 请求资源不存在")

        template_ids = resource_ids.get("template", [])
        templates = self._asset_records(session, template_ids)
        if any(
            record.asset_type != "other"
            or record.provenance.get("origin") != "playground_template"
            for record in templates.values()
        ):
            raise AIGatewayResourceNotFoundError("AI 请求资源不存在")

        frame_ids = resource_ids.get("storyboard_frame", [])
        if frame_ids:
            if project is None or any(
                not self._payload_contains_id(project.payload, identifier)
                for identifier in frame_ids
            ):
                raise AIGatewayResourceNotFoundError("AI 请求资源不存在")

    def authorize_in_session(self, session: Session) -> None:
        project = self._project(session)
        self._authorize_media(session)
        self._authorize_resources(session, project)


class AIGateway:
    def __init__(
        self,
        *,
        reservation: TicketReservationService,
        task_state: AITaskStateService,
        model_configuration: RoutePlanProvider,
        dispatcher: TaskDispatcher,
    ) -> None:
        self.reservation = reservation
        self.task_state = task_state
        self.model_configuration = model_configuration
        self.dispatcher = dispatcher

    @staticmethod
    def _task_config(plan: CapabilityRoutePlan) -> dict[str, Any]:
        primary = _route_payload(plan.primary)
        return {
            **primary,
            "tokens_per_ticket": plan.tokens_per_ticket,
            "routes": [_route_payload(route) for route in plan.routes],
        }

    @staticmethod
    def _submitted(
        reservation: ReservedTask,
        *,
        attempt_id: str | None,
        status: str,
        config_snapshot: Mapping[str, Any],
        dispatched: bool,
    ) -> SubmittedAITask:
        return SubmittedAITask(
            task_id=reservation.task_id,
            attempt_id=attempt_id,
            status=status,
            capability=reservation.capability,
            quoted_microtickets=reservation.quoted_microtickets,
            tokens_per_ticket=reservation.tokens_per_ticket,
            model_display_name=str(config_snapshot.get("display_name") or "平台模型"),
            provider_model_id=str(config_snapshot.get("provider_model_id") or ""),
            reused=reservation.reused,
            dispatched=dispatched,
        )

    def submit(
        self,
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> SubmittedAITask:
        request = AIGatewayRequestContract.parse(context, payload)
        reservation_payload = request.reservation_payload()
        project_id = str(request.project_id) if request.project_id else None
        reservation = self.reservation.find_existing(
            context,
            capability=request.capability.value,
            idempotency_key=request.idempotency_key,
            request_payload=reservation_payload,
            project_id=project_id,
        )
        if reservation is None:
            try:
                plan = self.model_configuration.build_plan(
                    request.capability.value,
                    request.parameters,
                )
                task_config = self._task_config(plan)
                maximum_tokens = maximum_metering_tokens(
                    task_config["metering_formula"],
                    parameters=task_config["parameters"],
                )
            except (LookupError, ValueError, MeteringValidationError) as exc:
                raise AIGatewayConfigurationError(str(exc)) from exc

            authorizer = GatewayResourceAuthorizer(context, request)
            reservation = self.reservation.reserve_task(
                context,
                capability=request.capability.value,
                idempotency_key=request.idempotency_key,
                request_payload=reservation_payload,
                config_snapshot=task_config,
                maximum_metering_tokens=maximum_tokens,
                tokens_per_ticket=plan.tokens_per_ticket,
                max_ai_concurrency_per_user=plan.max_ai_concurrency_per_user,
                project_id=project_id,
                validate_resources=authorizer.authorize_in_session,
            )

        aggregate = self.task_state.get(context, reservation.task_id)
        persisted_config = aggregate.task.config_snapshot
        attempt_id = aggregate.attempts[0].id if aggregate.attempts else None
        if attempt_id is None and aggregate.task.status not in TERMINAL_TASK_STATUSES:
            attempt = self.task_state.create_initial_attempt(
                context,
                task_id=reservation.task_id,
                config_snapshot=persisted_config,
                provider=str(persisted_config["provider"]),
                provider_model_id=str(persisted_config["provider_model_id"]),
            )
            attempt_id = attempt.id

        status = aggregate.task.status
        if status == "reserved":
            status = self.task_state.transition_task(
                context,
                task_id=reservation.task_id,
                expected_statuses={"reserved"},
                target_status="queued",
            ).status
        if status != "queued":
            return self._submitted(
                reservation,
                attempt_id=attempt_id,
                status=status,
                config_snapshot=persisted_config,
                dispatched=False,
            )

        try:
            self.dispatcher.dispatch(reservation.task_id)
        except Exception as exc:
            raise AIGatewayDispatchError(reservation.task_id) from exc
        return self._submitted(
            reservation,
            attempt_id=attempt_id,
            status=status,
            config_snapshot=persisted_config,
            dispatched=True,
        )
