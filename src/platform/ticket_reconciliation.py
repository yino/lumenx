from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select

from .contracts import UserContext
from .database import Database
from .db_models import (
    AITaskRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from .identifiers import parse_optional_database_id
from .settings import DeploymentMode, get_deployment_settings


class TicketReconciliationAuthorizationError(PermissionError):
    pass


class TicketReconciliationValidationError(ValueError):
    pass


IssueSeverity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class TicketReconciliationIssue:
    code: str
    severity: IssueSeverity
    message: str
    user_id: str
    workspace_id: str | None = None
    task_id: str | None = None
    hold_id: str | None = None
    ledger_id: str | None = None
    usage_event_id: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TicketReconciliationReport:
    started_at: datetime
    completed_at: datetime
    stale_before: datetime
    target_user_id: str | None
    wallets_scanned: int
    ledger_entries_scanned: int
    holds_scanned: int
    tasks_scanned: int
    usage_events_scanned: int
    issues: tuple[TicketReconciliationIssue, ...]
    issues_truncated: bool

    @property
    def is_consistent(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "stale_before": self.stale_before.isoformat(),
            "target_user_id": self.target_user_id,
            "wallets_scanned": self.wallets_scanned,
            "ledger_entries_scanned": self.ledger_entries_scanned,
            "holds_scanned": self.holds_scanned,
            "tasks_scanned": self.tasks_scanned,
            "usage_events_scanned": self.usage_events_scanned,
            "is_consistent": self.is_consistent,
            "issue_count": len(self.issues),
            "issues_truncated": self.issues_truncated,
            "issues": [asdict(issue) for issue in self.issues],
        }


TERMINAL_TASK_OUTCOMES: dict[str, str] = {
    "succeeded": "succeeded",
    "failed": "nonbillable_failure",
    "cancelled": "cancelled",
    "support_review": "billable_failure",
}


def _canonical_id(value: str | None, name: str) -> int | None:
    try:
        return parse_optional_database_id(value, field=name)
    except ValueError as exc:
        raise TicketReconciliationValidationError(f"{name}无效") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _correlated_usage_id(record: TicketLedgerRecord) -> tuple[int | None, bool]:
    if not isinstance(record.correlation, dict):
        return None, False
    raw_value = record.correlation.get("usage_event_id")
    if raw_value is None:
        return None, False
    try:
        return parse_optional_database_id(raw_value, field="用量记录 ID"), True
    except ValueError:
        return None, True


class TicketReconciliationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def reconcile(
        self,
        admin: UserContext,
        *,
        target_user_id: str | None = None,
        stale_after: timedelta = timedelta(hours=24),
        now: datetime | None = None,
        max_issues: int = 1000,
    ) -> TicketReconciliationReport:
        if not admin.is_platform_admin:
            raise TicketReconciliationAuthorizationError("仅平台管理员可执行算力券对账")
        if stale_after <= timedelta(0):
            raise TicketReconciliationValidationError("陈旧预扣阈值必须大于零")
        if max_issues < 1 or max_issues > 100_000:
            raise TicketReconciliationValidationError("对账问题数量上限无效")

        target_id = _canonical_id(target_user_id, "目标用户标识")
        started_at = _utc(now or datetime.now(timezone.utc))
        stale_before = started_at - stale_after
        issues: list[TicketReconciliationIssue] = []
        issues_truncated = False

        def add_issue(issue: TicketReconciliationIssue) -> None:
            nonlocal issues_truncated
            if len(issues) < max_issues:
                issues.append(issue)
            else:
                issues_truncated = True

        def scoped(statement: Any, model: Any) -> Any:
            if target_id is None:
                return statement
            return statement.where(model.user_id == target_id)

        with self.database.transaction(admin) as session:
            wallets = list(
                session.scalars(
                    scoped(select(TicketWalletRecord), TicketWalletRecord)
                )
            )
            ledger_entries = list(
                session.scalars(
                    scoped(select(TicketLedgerRecord), TicketLedgerRecord)
                )
            )
            holds = list(
                session.scalars(scoped(select(TicketHoldRecord), TicketHoldRecord))
            )
            tasks = list(
                session.scalars(scoped(select(AITaskRecord), AITaskRecord))
            )
            usage_events = list(
                session.scalars(scoped(select(UsageEventRecord), UsageEventRecord))
            )

            ledger_available: dict[int, int] = defaultdict(int)
            ledger_held: dict[int, int] = defaultdict(int)
            ledger_granted: dict[int, int] = defaultdict(int)
            ledger_spent: dict[int, int] = defaultdict(int)
            ledger_usage_refs: dict[int, list[int]] = defaultdict(list)
            usage_ids = {event.id for event in usage_events}

            for record in ledger_entries:
                ledger_available[record.user_id] += record.available_delta
                ledger_held[record.user_id] += record.held_delta
                if (
                    record.entry_type in {"grant", "adjustment", "compensation"}
                    and record.available_delta > 0
                ):
                    ledger_granted[record.user_id] += record.available_delta
                if record.entry_type == "settlement":
                    ledger_spent[record.user_id] += record.amount_microtickets

                usage_id, has_reference = _correlated_usage_id(record)
                if has_reference and usage_id is None:
                    add_issue(
                        TicketReconciliationIssue(
                            code="LEDGER_USAGE_CORRELATION_INVALID",
                            severity="error",
                            message="账务流水包含无效的用量关联标识",
                            user_id=str(record.user_id),
                            workspace_id=(
                                str(record.workspace_id)
                                if record.workspace_id is not None
                                else None
                            ),
                            task_id=(str(record.task_id) if record.task_id else None),
                            ledger_id=str(record.id),
                        )
                    )
                elif usage_id is not None:
                    ledger_usage_refs[usage_id].append(record.id)
                    if usage_id not in usage_ids:
                        add_issue(
                            TicketReconciliationIssue(
                                code="LEDGER_USAGE_CORRELATION_MISSING",
                                severity="error",
                                message="账务流水引用的用量记录不存在",
                                user_id=str(record.user_id),
                                workspace_id=(
                                    str(record.workspace_id)
                                    if record.workspace_id is not None
                                    else None
                                ),
                                task_id=(str(record.task_id) if record.task_id else None),
                                ledger_id=str(record.id),
                                usage_event_id=str(usage_id),
                            )
                        )

            open_hold_totals: dict[int, int] = defaultdict(int)
            tasks_by_id = {task.id: task for task in tasks}
            for hold in holds:
                if hold.status == "held":
                    open_hold_totals[hold.user_id] += hold.remaining_microtickets
                elif hold.remaining_microtickets != 0:
                    add_issue(
                        TicketReconciliationIssue(
                            code="CLOSED_HOLD_HAS_REMAINDER",
                            severity="error",
                            message="已结束的预扣仍有未处理余额",
                            user_id=str(hold.user_id),
                            workspace_id=str(hold.workspace_id),
                            task_id=str(hold.task_id),
                            hold_id=str(hold.id),
                            details={"remaining_microtickets": hold.remaining_microtickets},
                        )
                    )

                task = tasks_by_id.get(hold.task_id)
                if task is None:
                    add_issue(
                        TicketReconciliationIssue(
                            code="HOLD_TASK_MISSING",
                            severity="error",
                            message="预扣关联的 AI 任务不存在",
                            user_id=str(hold.user_id),
                            workspace_id=str(hold.workspace_id),
                            task_id=str(hold.task_id),
                            hold_id=str(hold.id),
                        )
                    )
                    continue
                if hold.status == "held" and task.status in TERMINAL_TASK_OUTCOMES:
                    add_issue(
                        TicketReconciliationIssue(
                            code="TERMINAL_TASK_OPEN_HOLD",
                            severity="error",
                            message="终态 AI 任务仍保留未关闭的预扣",
                            user_id=str(hold.user_id),
                            workspace_id=str(hold.workspace_id),
                            task_id=str(hold.task_id),
                            hold_id=str(hold.id),
                            details={
                                "task_status": task.status,
                                "remaining_microtickets": hold.remaining_microtickets,
                            },
                        )
                    )
                if hold.status == "held" and _utc(hold.created_at) <= stale_before:
                    add_issue(
                        TicketReconciliationIssue(
                            code="STALE_OPEN_HOLD",
                            severity="warning",
                            message="预扣超过阈值仍未结算或释放",
                            user_id=str(hold.user_id),
                            workspace_id=str(hold.workspace_id),
                            task_id=str(hold.task_id),
                            hold_id=str(hold.id),
                            details={
                                "task_status": task.status,
                                "created_at": _utc(hold.created_at).isoformat(),
                                "remaining_microtickets": hold.remaining_microtickets,
                            },
                        )
                    )

            for wallet in wallets:
                expected = {
                    "available_microtickets": ledger_available[wallet.user_id],
                    "held_microtickets": ledger_held[wallet.user_id],
                    "lifetime_granted_microtickets": ledger_granted[wallet.user_id],
                    "lifetime_spent_microtickets": ledger_spent[wallet.user_id],
                }
                actual = {
                    "available_microtickets": wallet.available_microtickets,
                    "held_microtickets": wallet.held_microtickets,
                    "lifetime_granted_microtickets": wallet.lifetime_granted_microtickets,
                    "lifetime_spent_microtickets": wallet.lifetime_spent_microtickets,
                }
                if actual != expected:
                    add_issue(
                        TicketReconciliationIssue(
                            code="WALLET_LEDGER_CACHE_MISMATCH",
                            severity="error",
                            message="钱包缓存与追加式账务流水汇总不一致",
                            user_id=str(wallet.user_id),
                            details={"actual": actual, "expected": expected},
                        )
                    )
                if wallet.held_microtickets != open_hold_totals[wallet.user_id]:
                    add_issue(
                        TicketReconciliationIssue(
                            code="WALLET_HOLD_CACHE_MISMATCH",
                            severity="error",
                            message="钱包预扣缓存与未关闭预扣汇总不一致",
                            user_id=str(wallet.user_id),
                            details={
                                "wallet_held_microtickets": wallet.held_microtickets,
                                "open_hold_microtickets": open_hold_totals[wallet.user_id],
                            },
                        )
                    )

            usage_by_task: dict[int, set[str]] = defaultdict(set)
            for event in usage_events:
                usage_by_task[event.task_id].add(event.outcome)
                if event.id not in ledger_usage_refs:
                    add_issue(
                        TicketReconciliationIssue(
                            code="USAGE_LEDGER_CORRELATION_MISSING",
                            severity="error",
                            message="用量记录缺少对应的账务流水关联",
                            user_id=str(event.user_id),
                            workspace_id=str(event.workspace_id),
                            task_id=str(event.task_id),
                            usage_event_id=str(event.id),
                        )
                    )

            for task in tasks:
                expected_outcome = TERMINAL_TASK_OUTCOMES.get(task.status)
                if expected_outcome is None:
                    continue
                outcomes = usage_by_task.get(task.id, set())
                if expected_outcome not in outcomes:
                    add_issue(
                        TicketReconciliationIssue(
                            code="TERMINAL_TASK_USAGE_MISSING",
                            severity="error",
                            message="终态 AI 任务缺少匹配的用量记录",
                            user_id=str(task.user_id),
                            workspace_id=str(task.workspace_id),
                            task_id=str(task.id),
                            details={
                                "task_status": task.status,
                                "expected_outcome": expected_outcome,
                                "actual_outcomes": sorted(outcomes),
                            },
                        )
                    )

        completed_at = datetime.now(timezone.utc)
        return TicketReconciliationReport(
            started_at=started_at,
            completed_at=completed_at,
            stale_before=stale_before,
            target_user_id=str(target_id) if target_id is not None else None,
            wallets_scanned=len(wallets),
            ledger_entries_scanned=len(ledger_entries),
            holds_scanned=len(holds),
            tasks_scanned=len(tasks),
            usage_events_scanned=len(usage_events),
            issues=tuple(issues),
            issues_truncated=issues_truncated,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="核验云端算力券与 AI 用量账务")
    parser.add_argument("--admin-user-id", required=True)
    parser.add_argument("--target-user-id")
    parser.add_argument("--stale-after-minutes", type=int, default=24 * 60)
    parser.add_argument("--max-issues", type=int, default=1000)
    args = parser.parse_args()

    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("算力券对账仅支持已配置数据库的云端模式")
    database = Database(settings.database_url)
    try:
        report = TicketReconciliationService(database).reconcile(
            UserContext(
                user_id=args.admin_user_id,
                session_id="ticket-reconciliation",
                is_platform_admin=True,
            ),
            target_user_id=args.target_user_id,
            stale_after=timedelta(minutes=args.stale_after_minutes),
            max_issues=args.max_issues,
        )
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.is_consistent else 2
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
