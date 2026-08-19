from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from sqlalchemy import insert
from sqlalchemy.orm import Session

from .contracts import AdminContext, UserContext
from .credentials import reject_plaintext_secrets
from .database import Database
from .db_models import AuditEventRecord
from .error_protocol import request_correlation_id
from .identifiers import parse_database_id, parse_optional_database_id


_FORBIDDEN_SUMMARY_KEYS = {
    "content",
    "credential",
    "credentials",
    "password",
    "prompt",
    "raw_payload",
    "request_payload",
    "secret",
    "token",
}


def _safe_summary(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    summary = dict(value)
    for key in summary:
        normalized = key.strip().lower()
        if normalized in _FORBIDDEN_SUMMARY_KEYS or any(
            marker in normalized for marker in ("password", "secret", "credential")
        ):
            raise ValueError("审计摘要不允许包含敏感字段或用户原始内容")
    reject_plaintext_secrets(summary, path="审计摘要")
    try:
        encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("审计摘要必须是 JSON 数据") from exc
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("审计摘要过大")
    return summary


def request_network_fingerprint(request: Request) -> str:
    source = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")[:256]
    return hashlib.sha256(f"{source}|{user_agent}".encode("utf-8")).hexdigest()


def append_audit_event(session: Session, **values: Any) -> None:
    """Append an audit row without requesting it back through SELECT RLS."""
    session.execute(insert(AuditEventRecord.__table__).inline().values(**values))


class AuditService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        identity: UserContext | AdminContext,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        request: Request,
        target_user_id: str | None = None,
        workspace_id: str | None = None,
        reason: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        if not action.strip() or len(action) > 120:
            raise ValueError("审计动作名称无效")
        if not target_type.strip() or len(target_type) > 80:
            raise ValueError("审计目标类型无效")
        if isinstance(identity, AdminContext):
            actor_user_id = None
            actor_admin_id = parse_database_id(identity.admin_id, field="操作管理员 ID")
            canonical_target_user = (
                parse_database_id(target_user_id, field="目标用户 ID")
                if target_user_id
                else None
            )
        else:
            actor_user_id = parse_database_id(identity.user_id, field="操作用户 ID")
            actor_admin_id = None
            canonical_target_user = (
                parse_database_id(target_user_id, field="目标用户 ID")
                if target_user_id
                else actor_user_id
            )
        canonical_workspace = parse_optional_database_id(
            workspace_id,
            field="工作区 ID",
        )
        with self.database.transaction(identity) as session:
            append_audit_event(
                session,
                actor_user_id=actor_user_id,
                actor_admin_id=actor_admin_id,
                target_user_id=canonical_target_user,
                workspace_id=canonical_workspace,
                action=action.strip(),
                target_type=target_type.strip(),
                target_id=target_id,
                reason=reason.strip() if reason else None,
                before_summary=_safe_summary(before),
                after_summary=_safe_summary(after),
                correlation_id=request_correlation_id(request),
                network_fingerprint=request_network_fingerprint(request),
            )
