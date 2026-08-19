from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from starlette.responses import Response

from ..contracts import AdminContext
from ..database import (
    Database,
    set_transaction_admin_context,
    set_transaction_admin_login_username,
    set_transaction_admin_session_token_hash,
)
from ..db_models import AdminSessionRecord, AdminUserRecord
from .security import InvalidUsernameError, PasswordService, normalize_username
from .sessions import (
    SessionAuthenticationError,
    SessionPolicy,
    as_utc,
    hash_session_secret,
)


ADMIN_SESSION_COOKIE_NAME = "lumenx_admin_session"
ADMIN_CSRF_COOKIE_NAME = "lumenx_admin_csrf"


class AdminCredentialError(ValueError):
    pass


class AdminAccountSuspendedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminIssuedSession:
    record: AdminSessionRecord
    token: str
    csrf_token: str
    policy: SessionPolicy


@dataclass(frozen=True, slots=True)
class AdminSessionPrincipal:
    admin_id: int
    session_id: int
    username: str
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class AdminLoginResult:
    admin_id: int
    username: str
    must_change_password: bool
    session: AdminIssuedSession


def issue_admin_session(
    admin_id: int,
    session_secret: str,
    *,
    policy: SessionPolicy,
    now: datetime | None = None,
    network_fingerprint: str | None = None,
    user_agent: str | None = None,
) -> AdminIssuedSession:
    if len(session_secret) < 32:
        raise ValueError("管理员会话密钥至少需要 32 个字符")
    issued_at = now or datetime.now(UTC)
    token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    record = AdminSessionRecord(
        admin_user_id=admin_id,
        token_hash=hash_session_secret(token, session_secret),
        csrf_token_hash=hash_session_secret(csrf_token, session_secret),
        created_at=issued_at,
        last_seen_at=issued_at,
        idle_expires_at=issued_at + timedelta(seconds=policy.idle_seconds),
        absolute_expires_at=issued_at + timedelta(seconds=policy.absolute_seconds),
        idle_timeout_seconds=policy.idle_seconds,
        network_fingerprint=network_fingerprint,
        user_agent=user_agent[:512] if user_agent else None,
    )
    return AdminIssuedSession(record=record, token=token, csrf_token=csrf_token, policy=policy)


def set_admin_session_cookies(
    response: Response,
    issued: AdminIssuedSession,
    *,
    secure: bool = True,
) -> None:
    response.set_cookie(
        ADMIN_SESSION_COOKIE_NAME,
        issued.token,
        max_age=issued.policy.absolute_seconds,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        ADMIN_CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=issued.policy.absolute_seconds,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def clear_admin_session_cookies(response: Response, *, secure: bool = True) -> None:
    response.delete_cookie(
        ADMIN_SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        ADMIN_CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


class AdminSessionService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        policy: SessionPolicy | None = None,
    ) -> None:
        if len(session_secret) < 32:
            raise ValueError("管理员会话密钥至少需要 32 个字符")
        self.database = database
        self.session_secret = session_secret
        self.policy = policy or SessionPolicy()

    def resolve(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        now: datetime | None = None,
    ) -> AdminSessionPrincipal:
        checked_at = now or datetime.now(UTC)
        token_hash = hash_session_secret(token, self.session_secret)
        failure: SessionAuthenticationError | None = None
        principal: AdminSessionPrincipal | None = None
        with self.database.transaction() as session:
            set_transaction_admin_session_token_hash(session, token_hash)
            record = session.scalar(
                select(AdminSessionRecord)
                .where(AdminSessionRecord.token_hash == token_hash)
                .limit(1)
            )
            if record is None or record.revoked_at is not None:
                failure = SessionAuthenticationError(
                    "ADMIN_AUTH_REQUIRED", "管理员登录状态已失效，请重新登录"
                )
            else:
                identity = AdminContext(
                    admin_id=str(record.admin_user_id),
                    session_id=str(record.id),
                )
                set_transaction_admin_context(session, identity)
                admin = session.get(AdminUserRecord, record.admin_user_id)
                if admin is None:
                    failure = SessionAuthenticationError(
                        "ADMIN_AUTH_REQUIRED", "管理员登录状态已失效，请重新登录"
                    )
                elif admin.status != "active":
                    record.revoked_at = checked_at
                    failure = SessionAuthenticationError(
                        "ADMIN_ACCOUNT_SUSPENDED", "管理员账号已停用"
                    )
                elif checked_at >= as_utc(record.idle_expires_at) or checked_at >= as_utc(
                    record.absolute_expires_at
                ):
                    record.revoked_at = checked_at
                    failure = SessionAuthenticationError(
                        "ADMIN_SESSION_EXPIRED", "管理员登录已过期，请重新登录"
                    )
                elif csrf_token is not None and not hmac.compare_digest(
                    record.csrf_token_hash,
                    hash_session_secret(csrf_token, self.session_secret),
                ):
                    failure = SessionAuthenticationError(
                        "ADMIN_CSRF_INVALID", "管理员安全校验失败，请刷新后重试"
                    )
                else:
                    record.last_seen_at = checked_at
                    idle_seconds = record.idle_timeout_seconds or self.policy.idle_seconds
                    record.idle_expires_at = min(
                        checked_at + timedelta(seconds=idle_seconds),
                        as_utc(record.absolute_expires_at),
                    )
                    principal = AdminSessionPrincipal(
                        admin_id=admin.id,
                        session_id=record.id,
                        username=admin.username,
                        must_change_password=admin.must_change_password,
                    )
        if failure is not None:
            raise failure
        if principal is None:
            raise SessionAuthenticationError(
                "ADMIN_AUTH_REQUIRED", "管理员登录状态已失效，请重新登录"
            )
        return principal

    def logout(self, token: str, *, now: datetime | None = None) -> None:
        revoked_at = now or datetime.now(UTC)
        token_hash = hash_session_secret(token, self.session_secret)
        with self.database.transaction() as session:
            set_transaction_admin_session_token_hash(session, token_hash)
            record = session.scalar(
                select(AdminSessionRecord)
                .where(AdminSessionRecord.token_hash == token_hash)
                .limit(1)
            )
            if record is None:
                return
            set_transaction_admin_context(
                session,
                AdminContext(
                    admin_id=str(record.admin_user_id),
                    session_id=str(record.id),
                ),
            )
            if record.revoked_at is None:
                record.revoked_at = revoked_at


class AdminAuthenticationService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        password_service: PasswordService | None = None,
        session_policy: SessionPolicy | None = None,
    ) -> None:
        self.database = database
        self.session_secret = session_secret
        self.password_service = password_service or PasswordService()
        self.session_policy = session_policy or SessionPolicy()
        self._dummy_password_hash = self.password_service.hash(
            "dummy-admin-login-password-2026"
        )

    def login(
        self,
        username: str,
        password: str,
        *,
        network_fingerprint: str | None = None,
        user_agent: str | None = None,
    ) -> AdminLoginResult:
        try:
            canonical_username = normalize_username(username)
        except InvalidUsernameError:
            self.password_service.verify(self._dummy_password_hash, password)
            raise AdminCredentialError("管理员账号或密码不正确") from None

        with self.database.transaction() as session:
            set_transaction_admin_login_username(session, canonical_username)
            admin = session.scalar(
                select(AdminUserRecord)
                .where(AdminUserRecord.username == canonical_username)
                .limit(1)
            )
            candidate_hash = admin.password_hash if admin is not None else self._dummy_password_hash
            if not self.password_service.verify(candidate_hash, password) or admin is None:
                raise AdminCredentialError("管理员账号或密码不正确")
            if admin.status != "active":
                raise AdminAccountSuspendedError("管理员账号已停用")
            issued = issue_admin_session(
                admin.id,
                self.session_secret,
                policy=self.session_policy,
                network_fingerprint=network_fingerprint,
                user_agent=user_agent,
            )
            set_transaction_admin_context(
                session,
                AdminContext(admin_id=str(admin.id), session_id="pending", username=admin.username),
            )
            session.add(issued.record)
            session.flush()
            return AdminLoginResult(
                admin_id=admin.id,
                username=admin.username,
                must_change_password=admin.must_change_password,
                session=issued,
            )

    def change_password(
        self,
        principal: AdminSessionPrincipal,
        current_password: str,
        new_password: str,
        *,
        now: datetime | None = None,
    ) -> None:
        changed_at = now or datetime.now(UTC)
        new_hash = self.password_service.hash(new_password)
        identity = AdminContext(
            admin_id=str(principal.admin_id),
            session_id=str(principal.session_id),
            username=principal.username,
        )
        with self.database.transaction(identity) as session:
            admin = session.get(AdminUserRecord, principal.admin_id)
            if admin is None or not self.password_service.verify(
                admin.password_hash, current_password
            ):
                raise AdminCredentialError("当前管理员密码不正确")
            admin.password_hash = new_hash
            admin.password_changed_at = changed_at
            admin.must_change_password = False
            session.execute(
                update(AdminSessionRecord)
                .where(
                    AdminSessionRecord.admin_user_id == principal.admin_id,
                    AdminSessionRecord.id != principal.session_id,
                    AdminSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=changed_at)
            )
