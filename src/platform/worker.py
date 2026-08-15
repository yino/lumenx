from celery import Celery
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .ai_dispatch import (
    AI_RECOVERY_TASK_NAME,
    AI_TASK_NAME,
    AI_TASK_QUEUE,
    CeleryRecoveryDispatcher,
)
from .ai_recovery import AIWorkerRecoveryService
from .ai_worker import AIWorkerService
from .database import Database
from .maintenance import (
    BACKUP_MAINTENANCE_TASK,
    MAINTENANCE_QUEUE,
    MEDIA_MAINTENANCE_TASK,
    RECONCILIATION_MAINTENANCE_TASK,
    RETENTION_MAINTENANCE_TASK,
    SESSION_MAINTENANCE_TASK,
    PlatformMaintenance,
    build_platform_maintenance,
)
from .media_storage import OSSPrivateObjectStore
from .settings import DeploymentMode, get_deployment_settings


settings = get_deployment_settings()

celery_app = Celery(
    "lumenx",
    broker=settings.redis_url,
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_default_queue=AI_TASK_QUEUE,
    task_routes={
        AI_TASK_NAME: {"queue": AI_TASK_QUEUE},
        AI_RECOVERY_TASK_NAME: {"queue": AI_TASK_QUEUE},
        SESSION_MAINTENANCE_TASK: {"queue": MAINTENANCE_QUEUE},
        RETENTION_MAINTENANCE_TASK: {"queue": MAINTENANCE_QUEUE},
        MEDIA_MAINTENANCE_TASK: {"queue": MAINTENANCE_QUEUE},
        RECONCILIATION_MAINTENANCE_TASK: {"queue": MAINTENANCE_QUEUE},
        BACKUP_MAINTENANCE_TASK: {"queue": MAINTENANCE_QUEUE},
    },
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "expire-sessions-every-15-minutes": {
            "task": SESSION_MAINTENANCE_TASK,
            "schedule": 15 * 60,
        },
        "cleanup-retention-daily": {
            "task": RETENTION_MAINTENANCE_TASK,
            "schedule": 24 * 60 * 60,
        },
        "cleanup-orphan-media-hourly": {
            "task": MEDIA_MAINTENANCE_TASK,
            "schedule": 60 * 60,
        },
        "reconcile-tasks-and-holds-every-10-minutes": {
            "task": RECONCILIATION_MAINTENANCE_TASK,
            "schedule": 10 * 60,
        },
        "verify-backup-daily": {
            "task": BACKUP_MAINTENANCE_TASK,
            "schedule": 24 * 60 * 60,
        },
    },
)


_worker_service: AIWorkerService | None = None
_recovery_service: AIWorkerRecoveryService | None = None
_maintenance_service: PlatformMaintenance | None = None


def _configure_release_test_worker() -> AIWorkerService:
    from .ai_io import AIOutputFinalizationService, AIProviderInputResolver
    from .ai_task_state import AITaskStateService
    from .ai_worker import AIWorkerTaskRepository
    from .media_storage import CloudMediaStorage
    from .test_adapters import (
        DeterministicModelClientFactory,
        DeterministicPrivateObjectStore,
        DeterministicProviderInvoker,
        DeterministicProviderOutputDownloader,
    )
    from .ticket_settlement import TicketSettlementService

    if not settings.database_url:
        raise RuntimeError("发布测试 worker 缺少 PostgreSQL 配置")
    database = Database(settings.database_url)
    storage = CloudMediaStorage(
        database,
        DeterministicPrivateObjectStore(settings),
        namespace_prefix="release-test",
    )
    task_state = AITaskStateService(database)
    settlement = TicketSettlementService(database)
    return AIWorkerService(
        tasks=AIWorkerTaskRepository(database),
        task_state=task_state,
        settlement=settlement,
        model_clients=DeterministicModelClientFactory(settings),
        provider_invoker=DeterministicProviderInvoker(settings),
        input_resolver=AIProviderInputResolver(storage),
        output_finalizer=AIOutputFinalizationService(
            task_state=task_state,
            settlement=settlement,
            media_storage=storage,
            downloader=DeterministicProviderOutputDownloader(settings),
        ),
    )


def _configure_cloud_worker() -> AIWorkerService:
    from .ai_io import AIOutputFinalizationService, AIProviderInputResolver
    from .ai_task_state import AITaskStateService
    from .ai_worker import AIWorkerTaskRepository
    from .credentials import EnvironmentCredentialProvider
    from .media_storage import CloudMediaStorage
    from .model_routing import RequestScopedModelClientFactory
    from .provider_runtime import (
        ProductionProviderInvoker,
        ProductionProviderOutputDownloader,
    )
    from .ticket_settlement import TicketSettlementService

    if not settings.database_url:
        raise RuntimeError("AI worker 缺少 PostgreSQL 配置")
    database = Database(settings.database_url)
    storage = CloudMediaStorage(database, OSSPrivateObjectStore(settings))
    task_state = AITaskStateService(database)
    settlement = TicketSettlementService(database)
    return AIWorkerService(
        tasks=AIWorkerTaskRepository(database),
        task_state=task_state,
        settlement=settlement,
        model_clients=RequestScopedModelClientFactory(
            EnvironmentCredentialProvider(settings.provider_secret_ref_names)
        ),
        provider_invoker=ProductionProviderInvoker(settings),
        input_resolver=AIProviderInputResolver(storage),
        output_finalizer=AIOutputFinalizationService(
            task_state=task_state,
            settlement=settlement,
            media_storage=storage,
            downloader=ProductionProviderOutputDownloader(settings),
        ),
    )


if settings.deployment_mode is DeploymentMode.TEST:
    _worker_service = _configure_release_test_worker()
elif settings.deployment_mode is DeploymentMode.CLOUD:
    _worker_service = _configure_cloud_worker()


def configure_worker_service(service: AIWorkerService) -> None:
    global _worker_service
    _worker_service = service


def configure_recovery_service(service: AIWorkerRecoveryService) -> None:
    global _recovery_service
    _recovery_service = service


def configure_maintenance_service(service: PlatformMaintenance) -> None:
    global _maintenance_service
    _maintenance_service = service


def _maintenance() -> PlatformMaintenance:
    global _maintenance_service
    if _maintenance_service is not None:
        return _maintenance_service
    if not settings.database_url:
        raise RuntimeError("定时维护需要云端 PostgreSQL 配置")
    if settings.deployment_mode is DeploymentMode.TEST:
        from .test_adapters import DeterministicPrivateObjectStore

        object_store = DeterministicPrivateObjectStore(settings)
    else:
        object_store = OSSPrivateObjectStore(settings)
    _maintenance_service = build_platform_maintenance(
        Database(settings.database_url),
        object_store,
        CeleryRecoveryDispatcher(celery_app),
        backup_root=Path("/backups"),
    )
    return _maintenance_service


@celery_app.task(name=AI_TASK_NAME, ignore_result=True)
def execute_ai_task(task_id: str) -> dict[str, Any]:
    if _worker_service is None:
        raise RuntimeError("AI worker 服务尚未配置")
    return asdict(_worker_service.execute(task_id))


@celery_app.task(name=AI_RECOVERY_TASK_NAME, ignore_result=True)
def recover_ai_task(task_id: str) -> dict[str, Any]:
    if _recovery_service is None:
        raise RuntimeError("AI worker 恢复服务尚未配置")
    return asdict(_recovery_service.recover(task_id))


@celery_app.task(name=SESSION_MAINTENANCE_TASK, ignore_result=True)
def expire_sessions() -> dict[str, Any]:
    return PlatformMaintenance.serialize(_maintenance().sessions.run())


@celery_app.task(name=RETENTION_MAINTENANCE_TASK, ignore_result=True)
def cleanup_retention() -> dict[str, Any]:
    return PlatformMaintenance.serialize(_maintenance().retention.run())


@celery_app.task(name=MEDIA_MAINTENANCE_TASK, ignore_result=True)
def cleanup_media() -> dict[str, Any]:
    return PlatformMaintenance.serialize(_maintenance().media.run())


@celery_app.task(name=RECONCILIATION_MAINTENANCE_TASK, ignore_result=True)
def reconcile_tasks_and_holds() -> dict[str, Any]:
    return PlatformMaintenance.serialize(_maintenance().reconciliation.run())


@celery_app.task(name=BACKUP_MAINTENANCE_TASK, ignore_result=True)
def verify_backup() -> dict[str, Any]:
    return PlatformMaintenance.serialize(_maintenance().backup.run())
