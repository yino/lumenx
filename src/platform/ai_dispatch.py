from __future__ import annotations

from typing import Any, Protocol

from redis import Redis

from .observability import events, metrics
from .identifiers import parse_database_id

AI_TASK_NAME = "lumenx.ai.execute"
AI_RECOVERY_TASK_NAME = "lumenx.ai.recover"
AI_TASK_QUEUE = "ai"


class CelerySender(Protocol):
    def send_task(self, name: str, **options: Any) -> Any: ...


class CeleryTaskDispatcher:
    """Dispatch only the authoritative PostgreSQL task identifier."""

    def __init__(self, celery_app: CelerySender) -> None:
        self.celery_app = celery_app

    def dispatch(self, task_id: str) -> None:
        try:
            canonical_task_id = str(parse_database_id(task_id, field="任务 ID"))
        except ValueError as exc:
            raise ValueError("AI 任务标识无效") from exc
        try:
            self.celery_app.send_task(
                AI_TASK_NAME,
                args=(canonical_task_id,),
                kwargs={},
                queue=AI_TASK_QUEUE,
                serializer="json",
                ignore_result=True,
            )
        except Exception as exc:
            metrics.increment(
                "ai_queue_dispatch_total",
                labels={"queue": AI_TASK_QUEUE, "outcome": "failed"},
            )
            events.emit(
                "ai.queue_dispatch_failed",
                task_id=canonical_task_id,
                queue=AI_TASK_QUEUE,
                error_type=type(exc).__name__,
            )
            raise
        metrics.increment(
            "ai_queue_dispatch_total",
            labels={"queue": AI_TASK_QUEUE, "outcome": "succeeded"},
        )


class CeleryRecoveryDispatcher(CeleryTaskDispatcher):
    def dispatch(self, task_id: str) -> None:
        try:
            canonical_task_id = str(parse_database_id(task_id, field="任务 ID"))
        except ValueError as exc:
            raise ValueError("AI 任务标识无效") from exc
        self.celery_app.send_task(
            AI_RECOVERY_TASK_NAME,
            args=(canonical_task_id,),
            kwargs={},
            queue=AI_TASK_QUEUE,
            serializer="json",
            ignore_result=True,
        )
        metrics.increment(
            "ai_queue_dispatch_total",
            labels={"queue": AI_TASK_QUEUE, "outcome": "recovery"},
        )


def observe_queue_depth(redis_client: Redis, *, queue: str = AI_TASK_QUEUE) -> int:
    depth = int(redis_client.llen(queue))
    if depth < 0:
        raise ValueError("队列深度不能为负数")
    metrics.gauge("ai_queue_depth", depth, labels={"queue": queue})
    return depth
