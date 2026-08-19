from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import select, update

from ..audit import append_audit_event
from ..contracts import UserContext
from ..database import (
    Database,
    set_transaction_login_phone,
    set_transaction_login_username,
    set_transaction_user_context,
)
from ..db_models import AuthSessionRecord, UserRecord, WorkspaceRecord
from .security import (
    InvalidPhoneError,
    InvalidUsernameError,
    PasswordService,
    normalize_login_identifier,
)
from .sessions import IssuedSession, SessionPolicy, SessionPrincipal, issue_session


class CredentialError(ValueError):
    pass


class AccountSuspendedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoginResult:
    user_id: int
    workspace_id: int
    phone_canonical: str | None
    username: str | None
    phone_verified: bool
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
        identifier: str,
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
            identifier_kind, canonical_identifier = normalize_login_identifier(identifier)
        except (InvalidPhoneError, InvalidUsernameError):
            self.password_service.verify(self._dummy_password_hash, password)
            raise CredentialError("账号或密码不正确") from None

        issued: IssuedSession | None = None
        workspace_id: int | None = None
        user: UserRecord | None = None
        with self.database.transaction() as session:
            if identifier_kind == "phone":
                set_transaction_login_phone(session, canonical_identifier)
                predicate = UserRecord.phone_canonical == canonical_identifier
            else:
                set_transaction_login_username(session, canonical_identifier)
                predicate = UserRecord.username == canonical_identifier
            user = session.scalar(select(UserRecord).where(predicate).limit(1))
            candidate_hash = user.password_hash if user is not None else self._dummy_password_hash
            password_valid = self.password_service.verify(candidate_hash, password)
            if user is None or not password_valid:
                raise CredentialError("账号或密码不正确")

            # PostgreSQL applies UPDATE RLS to SELECT FOR UPDATE. Establish only
            # self scope after credential verification, then lock and re-check.
            set_transaction_user_context(
                session,
                UserContext(
                    user_id=str(user.id),
                    session_id="login-pending",
                ),
            )
            locked_user = session.scalar(
                select(UserRecord).where(UserRecord.id == user.id).limit(1).with_for_update()
            )
            if locked_user is None or not self.password_service.verify(
                locked_user.password_hash,
                password,
            ):
                raise CredentialError("账号或密码不正确")
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
                    append_audit_event(
                        session,
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
            username=user.username,
            phone_verified=user.phone_verified_at is not None,
            session=issued,
        )

    def default_workspace_id(self, principal: SessionPrincipal) -> int:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
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
