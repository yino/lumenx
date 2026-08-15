from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import UserContext
from ..database import Database, set_transaction_invitation_hash
from ..db_models import AuditEventRecord, RegistrationInvitationRecord
from ..identifiers import parse_database_id
from .security import normalize_phone


class InvitationError(ValueError):
    def __init__(self, code: str, message: str = "邀请码无效或已失效") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    record: RegistrationInvitationRecord
    plaintext_code: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def hash_invitation_secret(value: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"lumenx-registration-invitation:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class InvitationService:
    def __init__(self, database: Database, invitation_secret: str) -> None:
        if len(invitation_secret) < 32:
            raise ValueError("邀请码密钥至少需要 32 个字符")
        self.database = database
        self.invitation_secret = invitation_secret

    @staticmethod
    def _require_admin(identity: UserContext) -> None:
        if not identity.is_platform_admin:
            raise PermissionError("仅平台管理员可以管理注册邀请")

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写邀请操作原因")
        return normalized

    @staticmethod
    def _audit(
        session: Session,
        identity: UserContext,
        record: RegistrationInvitationRecord,
        *,
        action: str,
        reason: str,
        correlation_id: str | None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        session.add(
            AuditEventRecord(
                actor_user_id=parse_database_id(identity.user_id, field="管理员 ID"),
                action=action,
                target_type="registration_invitation",
                target_id=str(record.id),
                reason=reason,
                before_summary=before,
                after_summary=after,
                correlation_id=correlation_id or str(uuid.uuid4()),
            )
        )

    def issue(
        self,
        identity: UserContext,
        *,
        phone: str,
        expires_at: datetime,
        reason: str,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> IssuedInvitation:
        self._require_admin(identity)
        checked_at = _utc(now or datetime.now(UTC))
        if expires_at.tzinfo is None:
            raise ValueError("邀请过期时间必须包含时区")
        expires_at = _utc(expires_at)
        if expires_at <= checked_at or expires_at > checked_at + timedelta(days=90):
            raise ValueError("邀请有效期必须在未来 90 天内")
        normalized_reason = self._reason(reason)
        plaintext_code = secrets.token_urlsafe(32)
        record = RegistrationInvitationRecord(
            phone_canonical=normalize_phone(phone),
            invitation_hash=hash_invitation_secret(
                plaintext_code, self.invitation_secret
            ),
            created_by_admin_id=parse_database_id(identity.user_id, field="管理员 ID"),
            issue_reason=normalized_reason,
            status="active",
            expires_at=expires_at,
        )
        with self.database.transaction(identity) as session:
            session.add(record)
            session.flush()
            self._audit(
                session,
                identity,
                record,
                action="registration.invitation.create",
                reason=normalized_reason,
                correlation_id=correlation_id,
                after={
                    "phone_fingerprint": hashlib.sha256(
                        record.phone_canonical.encode("utf-8")
                    ).hexdigest(),
                    "expires_at": record.expires_at.isoformat(),
                    "status": record.status,
                },
            )
        return IssuedInvitation(record=record, plaintext_code=plaintext_code)

    def list(self, identity: UserContext, *, now: datetime | None = None) -> list[RegistrationInvitationRecord]:
        self._require_admin(identity)
        checked_at = _utc(now or datetime.now(UTC))
        with self.database.transaction(identity) as session:
            records = list(
                session.scalars(
                    select(RegistrationInvitationRecord).order_by(
                        RegistrationInvitationRecord.created_at.desc(),
                        RegistrationInvitationRecord.id.desc(),
                    )
                )
            )
            for record in records:
                if record.status == "active" and _utc(record.expires_at) <= checked_at:
                    record.status = "expired"
            session.flush()
            return records

    def revoke(
        self,
        identity: UserContext,
        invitation_id: int,
        *,
        reason: str,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> RegistrationInvitationRecord:
        self._require_admin(identity)
        normalized_reason = self._reason(reason)
        revoked_at = _utc(now or datetime.now(UTC))
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(RegistrationInvitationRecord)
                .where(RegistrationInvitationRecord.id == invitation_id)
                .with_for_update()
            )
            if record is None:
                raise LookupError("注册邀请不存在")
            if record.status != "active":
                raise InvitationError("INVITATION_NOT_ACTIVE", "仅未使用的有效邀请可以撤销")
            before = {"status": record.status}
            record.status = "revoked"
            record.revoked_at = revoked_at
            record.revoke_reason = normalized_reason
            self._audit(
                session,
                identity,
                record,
                action="registration.invitation.revoke",
                reason=normalized_reason,
                correlation_id=correlation_id,
                before=before,
                after={"status": record.status},
            )
            session.flush()
            return record

    def require_for_registration(
        self,
        session: Session,
        *,
        phone_canonical: str,
        plaintext_code: str | None,
        now: datetime,
    ) -> RegistrationInvitationRecord:
        if not plaintext_code or len(plaintext_code) > 256:
            raise InvitationError("INVITATION_INVALID")
        invitation_hash = hash_invitation_secret(
            plaintext_code, self.invitation_secret
        )
        set_transaction_invitation_hash(session, invitation_hash)
        record = session.scalar(
            select(RegistrationInvitationRecord)
            .where(RegistrationInvitationRecord.invitation_hash == invitation_hash)
            .with_for_update()
        )
        if record is None:
            raise InvitationError("INVITATION_INVALID")
        if record.status != "active":
            raise InvitationError("INVITATION_INVALID")
        if _utc(record.expires_at) <= _utc(now):
            record.status = "expired"
            raise InvitationError("INVITATION_INVALID")
        if not hmac.compare_digest(record.phone_canonical, phone_canonical):
            raise InvitationError("INVITATION_INVALID")
        return record

    @staticmethod
    def consume(
        record: RegistrationInvitationRecord,
        *,
        user_id: int,
        consumed_at: datetime,
    ) -> None:
        record.status = "consumed"
        record.consumed_by_user_id = user_id
        record.consumed_at = consumed_at
