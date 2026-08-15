from __future__ import annotations

import pytest

from src.platform.ai_dispatch import (
    AI_RECOVERY_TASK_NAME,
    AI_TASK_NAME,
    AI_TASK_QUEUE,
    CeleryRecoveryDispatcher,
    CeleryTaskDispatcher,
)
from src.platform.worker import celery_app


class RecordingCelery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send_task(self, name: str, **options):
        self.calls.append((name, options))


def test_dispatch_message_contains_only_postgres_task_id() -> None:
    celery = RecordingCelery()
    dispatcher = CeleryTaskDispatcher(celery)
    task_id = "123"

    dispatcher.dispatch(task_id)

    assert celery.calls == [
        (
            AI_TASK_NAME,
            {
                "args": (task_id,),
                "kwargs": {},
                "queue": AI_TASK_QUEUE,
                "serializer": "json",
                "ignore_result": True,
            },
        )
    ]


def test_dispatch_rejects_non_integer_task_identifier() -> None:
    celery = RecordingCelery()

    with pytest.raises(ValueError, match="任务标识无效"):
        CeleryTaskDispatcher(celery).dispatch("not-a-task-id")

    assert celery.calls == []


def test_celery_uses_ai_queue_without_redis_result_authority() -> None:
    assert celery_app.conf.task_default_queue == AI_TASK_QUEUE
    assert celery_app.conf.task_routes[AI_TASK_NAME] == {"queue": AI_TASK_QUEUE}
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.task_store_errors_even_if_ignored is False
    assert celery_app.backend.as_uri() == "disabled://"


def test_recovery_dispatch_also_contains_only_postgres_task_id() -> None:
    celery = RecordingCelery()
    task_id = "123"

    CeleryRecoveryDispatcher(celery).dispatch(task_id)

    assert celery.calls == [
        (
            AI_RECOVERY_TASK_NAME,
            {
                "args": (task_id,),
                "kwargs": {},
                "queue": AI_TASK_QUEUE,
                "serializer": "json",
                "ignore_result": True,
            },
        )
    ]
    assert celery_app.conf.task_routes[AI_RECOVERY_TASK_NAME] == {
        "queue": AI_TASK_QUEUE
    }
