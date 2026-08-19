from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..contracts import AdminContext, UserContext
from ..database import Database, set_transaction_reset_token_hash, set_transaction_user_context
from ..db_models import (
    AuditEventRecord,
    AuthSessionRecord,
    PasswordResetCredentialRecord,
    UserRecord,
    WorkspaceRecord,
)
from ..identifiers import parse_database_id
from ..ticket_wallet import TicketWalletService
from .security import (
    DuplicatePhoneError,
    PasswordService,
    ensure_phone_available,
    normalize_phone,
)
from .sessions import hash_session_secret


class AdminAuthorizationError(PermissionError):
    pass


class UserNotFoundError(LookupError):
    pass


class ResetCredentialError(ValueError):
    pass


class ProtectedAdministratorActionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedResetCredential:
    credential: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedUser:
    user_id: int
    workspace_id: int
    phone_canonical: str


def _require_admin(identity: AdminContext) -> None:
    if not isinstance(identity, AdminContext):
        raise AdminAuthorizationError("仅平台管理员可以执行此操作")


class UserAdministrationService:
    def __init__(
        self,
        database: Database,
        reset_secret: str,
        *,
        password_service: PasswordService | None = None,
    ) -> None:
        if len(reset_secret) < 32:
            raise ValueError("重置凭据密钥至少需要 32 个字符")
        self.database = database
        self.reset_secret = reset_secret
        self.password_service = password_service or PasswordService()

    def create_user(
        self,
        admin: AdminContext,
        phone: str,
        password: str,
        reason: str,
        *,
        initial_grant_microtickets: int = 0,
        config_version_id: str | None = None,
        default_workspace_name: str = "默认工作区",
        correlation_id: str | None = None,
    ) -> CreatedUser:
        _require_admin(admin)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("创建用户必须填写原因")
        workspace_name = default_workspace_name.strip()
        if not workspace_name:
            raise ValueError("默认工作区名称不能为空")
        phone_canonical = normalize_phone(phone)
        password_hash = self.password_service.hash(password)
        admin_id = parse_database_id(admin.admin_id, field="管理员 ID")

        try:
            with self.database.transaction(admin) as session:
                ensure_phone_available(session, phone_canonical)
                user = UserRecord(
                    phone_canonical=phone_canonical,
                    password_hash=password_hash,
                    status="active",
                    phone_verified_at=None,
                )
                session.add(user)
                session.flush()
                TicketWalletService.create_wallet_in_session(
                    session,
                    user.id,
                    initial_grant_microtickets,
                    reason="后台创建用户初始赠送",
                    actor_admin_id=admin_id,
                    correlation={
                        "source": "admin_user_creation",
                        "config_version_id": config_version_id,
                    },
                )
                workspace = WorkspaceRecord(user_id=user.id, name=workspace_name)
                session.add(workspace)
                session.flush()
                session.add(
                    AuditEventRecord(
                        actor_admin_id=admin_id,
                        target_user_id=user.id,
                        workspace_id=workspace.id,
                        action="user.create",
                        target_type="user",
                        target_id=str(user.id),
                        reason=normalized_reason,
                        after_summary={
                            "status": "active",
                            "workspace_created": True,
                            "initial_grant_microtickets": initial_grant_microtickets,
                            "config_version_id": config_version_id,
                        },
                        correlation_id=correlation_id or str(uuid.uuid4()),
                    )
                )
                created = CreatedUser(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    phone_canonical=user.phone_canonical,
                )
        except DuplicatePhoneError:
            raise
        except IntegrityError as exc:
            diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
            if getattr(diagnostic, "constraint_name", None) == "uq_users_phone_canonical":
                raise DuplicatePhoneError("该手机号已注册") from exc
            raise

        return created

    def set_status(
        self,
        admin: AdminContext,
        target_user_id: int,
        status: str,
        reason: str,
        correlation_id: str | None = None,
    ) -> None:
        _require_admin(admin)
        if status not in {"active", "suspended"}:
            raise ValueError("用户状态不正确")
        if not reason.strip():
            raise ValueError("必须填写操作原因")
        now = datetime.now(UTC)
        with self.database.transaction(admin) as session:
            user = session.get(UserRecord, target_user_id)
            if user is None:
                raise UserNotFoundError("用户不存在")
            previous_status = user.status
            user.status = status
            if status == "suspended":
                session.execute(
                    update(AuthSessionRecord)
                    .where(
                        AuthSessionRecord.user_id == target_user_id,
                        AuthSessionRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            session.add(
                AuditEventRecord(
                    actor_admin_id=parse_database_id(admin.admin_id, field="管理员 ID"),
                    target_user_id=target_user_id,
                    action="user.suspend" if status == "suspended" else "user.reactivate",
                    target_type="user",
                    target_id=str(target_user_id),
                    reason=reason.strip(),
                    before_summary={"status": previous_status},
                    after_summary={"status": status},
                    correlation_id=correlation_id or str(uuid.uuid4()),
                )
            )

    def revoke_sessions(
        self,
        admin: AdminContext,
        target_user_id: int,
        reason: str,
        correlation_id: str | None = None,
    ) -> int:
        _require_admin(admin)
        if not reason.strip():
            raise ValueError("必须填写操作原因")
        now = datetime.now(UTC)
        with self.database.transaction(admin) as session:
            user = session.get(UserRecord, target_user_id)
            if user is None:
                raise UserNotFoundError("用户不存在")
            result = session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.user_id == target_user_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            revoked_count = int(result.rowcount or 0)
            session.add(
                AuditEventRecord(
                    actor_admin_id=parse_database_id(admin.admin_id, field="管理员 ID"),
                    target_user_id=target_user_id,
                    action="user.sessions.revoke_all",
                    target_type="user",
                    target_id=str(target_user_id),
                    reason=reason.strip(),
                    after_summary={"revoked_session_count": revoked_count},
                    correlation_id=correlation_id or str(uuid.uuid4()),
                )
            )
            return revoked_count

    def issue_reset_credential(
        self,
        admin: AdminContext,
        target_user_id: int,
        reason: str,
        *,
        lifetime_seconds: int = 30 * 60,
        correlation_id: str | None = None,
    ) -> IssuedResetCredential:
        _require_admin(admin)
        if not reason.strip():
            raise ValueError("必须填写操作原因")
        if lifetime_seconds <= 0 or lifetime_seconds > 24 * 60 * 60:
            raise ValueError("重置凭据有效期不正确")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lifetime_seconds)
        credential = secrets.token_urlsafe(48)
        credential_hash = hash_session_secret(credential, self.reset_secret)
        with self.database.transaction(admin) as session:
            if session.get(UserRecord, target_user_id) is None:
                raise UserNotFoundError("用户不存在")
            session.add(
                PasswordResetCredentialRecord(
                    user_id=target_user_id,
                    created_by_admin_id=parse_database_id(admin.admin_id, field="管理员 ID"),
                    token_hash=credential_hash,
                    reason=reason.strip(),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            session.add(
                AuditEventRecord(
                    actor_admin_id=parse_database_id(admin.admin_id, field="管理员 ID"),
                    target_user_id=target_user_id,
                    action="user.password_reset.issue",
                    target_type="user",
                    target_id=str(target_user_id),
                    reason=reason.strip(),
                    after_summary={"expires_at": expires_at.isoformat()},
                    correlation_id=correlation_id or str(uuid.uuid4()),
                )
            )
        return IssuedResetCredential(credential=credential, expires_at=expires_at)


class SupportPasswordResetService:
    def __init__(
        self,
        database: Database,
        reset_secret: str,
        password_service: PasswordService | None = None,
    ) -> None:
        self.database = database
        self.reset_secret = reset_secret
        self.password_service = password_service or PasswordService()

    def reset_password(
        self,
        credential: str,
        new_password: str,
        *,
        now: datetime | None = None,
    ) -> None:
        checked_at = now or datetime.now(UTC)
        token_hash = hash_session_secret(credential, self.reset_secret)
        new_hash = self.password_service.hash(new_password)
        with self.database.transaction() as session:
            set_transaction_reset_token_hash(session, token_hash)
            record = session.scalar(
                select(PasswordResetCredentialRecord)
                .where(PasswordResetCredentialRecord.token_hash == token_hash)
                .limit(1)
            )
            if (
                record is None
                or record.used_at is not None
                or checked_at >= record.expires_at
            ):
                raise ResetCredentialError("重置凭据无效或已过期")
            identity = UserContext(user_id=str(record.user_id))
            set_transaction_user_context(session, identity)
            user = session.get(UserRecord, record.user_id)
            if user is None:
                raise ResetCredentialError("重置凭据无效或已过期")
            user.password_hash = new_hash
            user.password_changed_at = checked_at
            record.used_at = checked_at
            session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.user_id == record.user_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=checked_at)
            )
