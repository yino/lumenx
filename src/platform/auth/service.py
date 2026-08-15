from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import select, update

from ..contracts import UserContext
from ..database import Database, set_transaction_login_phone, set_transaction_user_context
from ..db_models import AuditEventRecord, AuthSessionRecord, UserRecord, WorkspaceRecord
from .security import InvalidPhoneError, PasswordService, normalize_phone
from .sessions import IssuedSession, SessionPolicy, SessionPrincipal, issue_session


class CredentialError(ValueError):
    pass


class AccountSuspendedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoginResult:
    user_id: int
    workspace_id: int
    phone_canonical: str
    phone_verified: bool
    is_platform_admin: bool
    session: IssuedSession


@dataclass(frozen=True, slots=True)
class AuthenticationPolicy:
    session_policy: SessionPolicy = SessionPolicy()
    max_sessions_per_user: int | None = None
    config_version_id: str | None = None


class AuthenticationService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        password_service: PasswordService | None = None,
        session_policy: SessionPolicy | None = None,
        policy_resolver: Callable[[], AuthenticationPolicy] | None = None,
    ) -> None:
        self.database = database
        self.session_secret = session_secret
        self.password_service = password_service or PasswordService()
        self.session_policy = session_policy or SessionPolicy()
        self.policy_resolver = policy_resolver
        self._dummy_password_hash = self.password_service.hash("dummy-login-password-2026")

    def login(
        self,
        phone: str,
        password: str,
        *,
        network_fingerprint: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        policy = (
            self.policy_resolver()
            if self.policy_resolver
            else AuthenticationPolicy(session_policy=self.session_policy)
        )
        try:
            phone_canonical = normalize_phone(phone)
        except InvalidPhoneError:
            self.password_service.verify(self._dummy_password_hash, password)
            raise CredentialError("手机号或密码不正确") from None

        issued: IssuedSession | None = None
        workspace_id: int | None = None
        user: UserRecord | None = None
        with self.database.transaction() as session:
            set_transaction_login_phone(session, phone_canonical)
            user = session.scalar(
                select(UserRecord).where(UserRecord.phone_canonical == phone_canonical).limit(1)
            )
            candidate_hash = user.password_hash if user is not None else self._dummy_password_hash
            password_valid = self.password_service.verify(candidate_hash, password)
            if user is None or not password_valid:
                raise CredentialError("手机号或密码不正确")

            # PostgreSQL applies UPDATE RLS to SELECT FOR UPDATE. Establish only
            # self scope after credential verification, then lock and re-check.
            set_transaction_user_context(
                session,
                UserContext(
                    user_id=str(user.id),
                    session_id="login-pending",
                    is_platform_admin=False,
                ),
            )
            locked_user = session.scalar(
                select(UserRecord).where(UserRecord.id == user.id).limit(1).with_for_update()
            )
            if locked_user is None or not self.password_service.verify(
                locked_user.password_hash,
                password,
            ):
                raise CredentialError("手机号或密码不正确")
            user = locked_user
            if user.status != "active":
                raise AccountSuspendedError("账号已停用")

            issued = issue_session(
                user.id,
                self.session_secret,
                policy=policy.session_policy,
                network_fingerprint=network_fingerprint,
                user_agent=user_agent,
                configuration_version_id=policy.config_version_id,
            )
            identity = UserContext(
                user_id=str(user.id),
                session_id="login-pending",
                is_platform_admin=user.is_platform_admin,
            )
            set_transaction_user_context(session, identity)
            if policy.max_sessions_per_user is not None:
                active_sessions = list(
                    session.scalars(
                        select(AuthSessionRecord)
                        .where(
                            AuthSessionRecord.user_id == user.id,
                            AuthSessionRecord.revoked_at.is_(None),
                        )
                        .order_by(
                            AuthSessionRecord.created_at.asc(),
                            AuthSessionRecord.id.asc(),
                        )
                        .with_for_update()
                    )
                )
                revoke_count = max(
                    len(active_sessions) + 1 - policy.max_sessions_per_user,
                    0,
                )
                revoked_at = datetime.now(UTC)
                for old_session in active_sessions[:revoke_count]:
                    old_session.revoked_at = revoked_at
                    session.add(
                        AuditEventRecord(
                            actor_user_id=user.id,
                            target_user_id=user.id,
                            action="auth.session.limit_revoke",
                            target_type="session",
                            target_id=str(old_session.id),
                            reason="登录时执行单用户会话上限",
                            before_summary={"revoked": False},
                            after_summary={
                                "revoked": True,
                                "max_sessions_per_user": policy.max_sessions_per_user,
                                "config_version_id": policy.config_version_id,
                            },
                            correlation_id=str(uuid.uuid4()),
                        )
                    )
            workspace_id = session.scalar(
                select(WorkspaceRecord.id)
                .where(
                    WorkspaceRecord.user_id == user.id,
                    WorkspaceRecord.deleted_at.is_(None),
                )
                .order_by(WorkspaceRecord.created_at.asc())
                .limit(1)
            )
            if workspace_id is None:
                raise RuntimeError("账号缺少可用工作区")
            if self.password_service.needs_rehash(user.password_hash):
                user.password_hash = self.password_service.hash(password)
            session.add(issued.record)
            session.flush()

        return LoginResult(
            user_id=user.id,
            workspace_id=workspace_id,
            phone_canonical=user.phone_canonical,
            phone_verified=user.phone_verified_at is not None,
            is_platform_admin=user.is_platform_admin,
            session=issued,
        )

    def default_workspace_id(self, principal: SessionPrincipal) -> int:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=principal.is_platform_admin,
        )
        with self.database.transaction(identity) as session:
            workspace_id = session.scalar(
                select(WorkspaceRecord.id)
                .where(
                    WorkspaceRecord.user_id == principal.user_id,
                    WorkspaceRecord.deleted_at.is_(None),
                )
                .order_by(WorkspaceRecord.created_at.asc())
                .limit(1)
            )
        if workspace_id is None:
            raise RuntimeError("账号缺少可用工作区")
        return workspace_id

    def change_password(
        self,
        principal: SessionPrincipal,
        current_password: str,
        new_password: str,
    ) -> None:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=principal.is_platform_admin,
        )
        new_hash = self.password_service.hash(new_password)
        changed_at = datetime.now(UTC)
        with self.database.transaction(identity) as session:
            user = session.get(UserRecord, principal.user_id)
            if user is None or not self.password_service.verify(
                user.password_hash, current_password
            ):
                raise CredentialError("当前密码不正确")
            user.password_hash = new_hash
            user.password_changed_at = changed_at
            session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.user_id == principal.user_id,
                    AuthSessionRecord.id != principal.session_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=changed_at)
            )
