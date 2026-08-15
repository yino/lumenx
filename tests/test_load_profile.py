from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.platform.db_models import AITaskRecord, TicketHoldRecord, TicketLedgerRecord
from src.platform.load_profile import InitialScaleTarget, simulate_backpressure
from src.platform.ticket_reservation import AIConcurrencyLimitError
from src.platform.worker import celery_app
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload


def test_initial_target_models_ten_thousand_users_and_daily_tasks() -> None:
    target = InitialScaleTarget()
    report = simulate_backpressure(target)

    assert target.registered_users == 10_000
    assert target.daily_ai_tasks == 10_000
    assert target.max_concurrency_per_user == 2
    assert report.submitted == 10_000
    assert report.admitted == 10_000
    assert report.completed == 10_000
    assert report.per_user_rejected == 0
    assert report.peak_running == target.global_worker_concurrency
    assert report.peak_queued == target.daily_ai_tasks - target.global_worker_concurrency


def test_hot_user_is_limited_to_two_while_queue_keeps_global_backpressure() -> None:
    target = InitialScaleTarget()
    hot_user_submissions = tuple([0] * 100 + list(range(1, 9901)))
    report = simulate_backpressure(target, submissions=hot_user_submissions)

    assert report.submitted == 10_000
    assert report.per_user_rejected == 98
    assert report.admitted == 9_902
    assert report.completed == 9_902
    assert report.peak_running == 8
    assert report.peak_queued > 9_000
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_gateway_rejects_third_active_task_without_extra_hold_or_ledger() -> None:
    database, context, media_id, asset_id = _gateway_database()
    dispatcher = RecordingDispatcher()
    gateway = _gateway(database, dispatcher)
    try:
        first = gateway.submit(
            context,
            _payload(media_id, asset_id, idempotency_key="concurrency-1"),
        )
        second = gateway.submit(
            context,
            _payload(media_id, asset_id, idempotency_key="concurrency-2"),
        )
        with pytest.raises(AIConcurrencyLimitError) as captured:
            gateway.submit(
                context,
                _payload(media_id, asset_id, idempotency_key="concurrency-3"),
            )

        assert captured.value.maximum_concurrency == 2
        assert dispatcher.task_ids == [first.task_id, second.task_id]
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 2
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 2
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 3
    finally:
        database.engine.dispose()
