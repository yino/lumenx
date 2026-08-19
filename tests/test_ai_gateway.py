from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.platform.ai_gateway import (
    AIGateway,
    AIGatewayDispatchError,
    AIGatewayResourceNotFoundError,
)
from src.platform.ai_task_state import AITaskStateService
from src.platform.contracts import ModelRouteSnapshot
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    AssetRecord,
    MediaObjectRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from src.platform.model_routing import CapabilityRoutePlan
from src.platform.ticket_reservation import (
    InsufficientTicketBalanceError,
    TicketReservationService,
)
from tests.test_content_repositories import _create_scope
from tests.test_ticket_reservation import _reservation_database


class RecordingDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.task_ids: list[str] = []

    def dispatch(self, task_id: str) -> None:
        self.task_ids.append(task_id)
        if self.fail:
            raise RuntimeError("queue unavailable")


class FixedRouteProvider:
    def __init__(self) -> None:
        self.requested_parameters = None
        self.version_id = str(uuid.uuid4())

    def build_plan(self, capability, requested_parameters):
        self.requested_parameters = dict(requested_parameters)
        parameters = {"count": 1, "resolution": "1024x1024"}
        parameters.update(requested_parameters)
        route = ModelRouteSnapshot(
            config_version_id=self.version_id,
            route_id=str(uuid.uuid4()),
            capability=str(capability),
            provider="dashscope",
            provider_model_id="wan-image-v1",
            display_name="平台图像模型",
            parameters=parameters,
            metering_formula={
                "kind": "image",
                "base_tokens": 10,
                "per_image_tokens": 100,
                "max_images": 4,
                "resolution_multipliers": {"1024x1024": 1},
                "option_multipliers": {},
            },
            fallback_policy={"enabled": False, "max_attempts": 1},
            secret_ref="DASHSCOPE_API_KEY",
        )
        return CapabilityRoutePlan(
            config_version_id=self.version_id,
            capability=str(capability),
            tokens_per_ticket=1000,
            max_ai_concurrency_per_user=2,
            routes=(route,),
        )


class UnavailableRouteProvider:
    def build_plan(self, capability, requested_parameters):
        raise LookupError("当前没有可用的模型配置")


class SpeechRouteProvider:
    def __init__(self) -> None:
        self.version_id = str(uuid.uuid4())

    def build_plan(self, capability, requested_parameters):
        route = ModelRouteSnapshot(
            config_version_id=self.version_id,
            route_id=str(uuid.uuid4()),
            capability=str(capability),
            provider="dashscope",
            provider_model_id="cosyvoice-v2",
            display_name="平台对白语音模型",
            parameters=dict(requested_parameters),
            metering_formula={
                "kind": "speech",
                "unit": "characters",
                "tokens_per_unit": 1,
                "max_units": 20_000,
            },
            fallback_policy={"enabled": False, "max_attempts": 1},
            secret_ref="DASHSCOPE_API_KEY",
        )
        return CapabilityRoutePlan(
            config_version_id=self.version_id,
            capability=str(capability),
            tokens_per_ticket=1000,
            max_ai_concurrency_per_user=2,
            routes=(route,),
        )


def _gateway_database():
    database, context = _reservation_database(initial_balance=3_000_000)
    AITaskAttemptRecord.__table__.create(database.engine)
    UsageEventRecord.__table__.create(database.engine)
    with database.transaction(context.identity) as session:
        media = MediaObjectRecord(
                user_id=int(context.identity.user_id),
                workspace_id=int(context.workspace_id),
                object_key=f"users/{context.identity.user_id}/input.png",
                mime_type="image/png",
                size_bytes=4,
                checksum_sha256="a" * 64,
                lifecycle_state="active",
                provenance={"origin": "upload"},
            )
        session.add(media)
        session.flush()
        asset = AssetRecord(
                user_id=int(context.identity.user_id),
                workspace_id=int(context.workspace_id),
                scope="workspace",
                asset_type="character",
                name="测试角色",
                payload={"id": str(uuid.uuid4()), "name": "测试角色"},
                provenance={"origin": "test"},
            )
        session.add(asset)
        session.flush()
        media_id = media.id
        asset_id = asset.id
    return database, context, media_id, asset_id


def _payload(media_id: int, asset_id: int, **updates):
    payload = {
        "capability": "image.i2i",
        "idempotency_key": "gateway-task-001",
        "resource_ids": {"asset": [str(asset_id)]},
        "media_ids": [str(media_id)],
        "content": "把角色绘制成电影分镜",
        "parameters": {"count": 1},
    }
    payload.update(updates)
    return payload


def _gateway(database, dispatcher, provider=None):
    return AIGateway(
        reservation=TicketReservationService(database),
        task_state=AITaskStateService(database),
        model_configuration=provider or FixedRouteProvider(),
        dispatcher=dispatcher,
    )


def test_gateway_validates_quotes_reserves_persists_attempt_and_dispatches_only_id() -> None:
    database, context, media_id, asset_id = _gateway_database()
    dispatcher = RecordingDispatcher()
    provider = FixedRouteProvider()
    try:
        result = _gateway(database, dispatcher, provider).submit(
            context,
            _payload(media_id, asset_id),
        )

        assert result.status == "queued"
        assert result.quoted_microtickets == 110_000
        assert result.tokens_per_ticket == 1000
        assert result.model_display_name == "平台图像模型"
        assert result.provider_model_id == "wan-image-v1"
        assert result.reused is False
        assert result.dispatched is True
        assert dispatcher.task_ids == [result.task_id]
        assert provider.requested_parameters == {"count": 1}
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(result.task_id))
            assert task is not None
            assert task.status == "queued"
            assert task.request_payload["input_media_ids"] == [str(media_id)]
            assert task.config_snapshot["parameters"] == {
                "count": 1,
                "resolution": "1024x1024",
            }
            assert task.config_snapshot["secret_ref"] == "DASHSCOPE_API_KEY"
            attempt = session.scalar(
                select(AITaskAttemptRecord).where(
                    AITaskAttemptRecord.task_id == task.id
                )
            )
            assert attempt is not None
            assert attempt.config_snapshot == task.config_snapshot
            assert attempt.provider_model_id == "wan-image-v1"
    finally:
        database.engine.dispose()


def test_gateway_quotes_speech_batch_from_submitted_characters() -> None:
    database, context, media_id, asset_id = _gateway_database()
    dispatcher = RecordingDispatcher()
    try:
        result = _gateway(database, dispatcher, SpeechRouteProvider()).submit(
            context,
            _payload(
                media_id,
                asset_id,
                capability="speech.tts",
                idempotency_key="speech-batch-quote-1",
                content={
                    "operation": "audio.dialogue.batch",
                    "items": [{"text": "第一句"}, {"text": "第二句台词"}],
                },
                parameters={},
            ),
        )

        assert result.quoted_microtickets == 8_000
        assert result.model_display_name == "平台对白语音模型"
        assert dispatcher.task_ids == [result.task_id]
    finally:
        database.engine.dispose()


def test_gateway_rejects_foreign_media_before_hold_or_task_creation() -> None:
    database, context, _media_id, asset_id = _gateway_database()
    foreign = _create_scope(database)
    with database.transaction(foreign.identity) as session:
        foreign_media = MediaObjectRecord(
                user_id=int(foreign.identity.user_id),
                workspace_id=int(foreign.workspace_id),
                object_key=f"users/{foreign.identity.user_id}/foreign.png",
                mime_type="image/png",
                size_bytes=4,
                checksum_sha256="b" * 64,
                lifecycle_state="active",
                provenance={"origin": "upload"},
            )
        session.add(foreign_media)
        session.flush()
        foreign_media_id = foreign_media.id
    try:
        with pytest.raises(AIGatewayResourceNotFoundError):
            _gateway(database, RecordingDispatcher()).submit(
                context,
                _payload(foreign_media_id, asset_id),
            )

        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    finally:
        database.engine.dispose()


def test_gateway_rejects_insufficient_balance_before_task_or_dispatch() -> None:
    database, context, media_id, asset_id = _gateway_database()
    dispatcher = RecordingDispatcher()
    with database.session_factory.begin() as session:
        wallet = session.get(
            TicketWalletRecord,
            int(context.identity.user_id),
        )
        assert wallet is not None
        wallet.available_microtickets = 0
    try:
        with pytest.raises(InsufficientTicketBalanceError) as captured:
            _gateway(database, dispatcher).submit(
                context,
                _payload(media_id, asset_id),
            )

        assert captured.value.available_microtickets == 0
        assert captured.value.required_microtickets == 110_000
        assert dispatcher.task_ids == []
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    finally:
        database.engine.dispose()


def test_dispatch_failure_keeps_one_queued_task_for_idempotent_redispatch() -> None:
    database, context, media_id, asset_id = _gateway_database()
    dispatcher = RecordingDispatcher(fail=True)
    gateway = _gateway(database, dispatcher)
    payload = _payload(media_id, asset_id)
    try:
        with pytest.raises(AIGatewayDispatchError) as captured:
            gateway.submit(context, payload)
        task_id = captured.value.task_id
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(task_id))
            assert task is not None
            assert task.status == "queued"
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1

        dispatcher.fail = False
        retried = gateway.submit(context, payload)

        assert retried.task_id == task_id
        assert retried.reused is True
        assert retried.dispatched is True
        assert dispatcher.task_ids == [task_id, task_id]
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
            assert session.scalar(select(func.count()).select_from(AITaskAttemptRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    finally:
        database.engine.dispose()


def test_idempotent_redispatch_uses_snapshot_when_active_route_is_unavailable() -> None:
    database, context, media_id, asset_id = _gateway_database()
    failed_dispatcher = RecordingDispatcher(fail=True)
    payload = _payload(media_id, asset_id)
    try:
        with pytest.raises(AIGatewayDispatchError) as captured:
            _gateway(database, failed_dispatcher).submit(context, payload)

        retry_dispatcher = RecordingDispatcher()
        retried = _gateway(
            database,
            retry_dispatcher,
            UnavailableRouteProvider(),
        ).submit(context, payload)

        assert retried.task_id == captured.value.task_id
        assert retried.reused is True
        assert retried.dispatched is True
        assert retried.model_display_name == "平台图像模型"
        assert retried.provider_model_id == "wan-image-v1"
        assert retry_dispatcher.task_ids == [retried.task_id]
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
            assert session.scalar(select(func.count()).select_from(AITaskAttemptRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    finally:
        database.engine.dispose()
