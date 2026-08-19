from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from starlette.responses import Response

from ..contracts import UserContext
from ..database import Database, set_transaction_session_token_hash, set_transaction_user_context
from ..db_models import UserRecord
from ..db_models import AuthSessionRecord
from ..identifiers import parse_database_id, parse_optional_database_id


SESSION_COOKIE_NAME = "lumenx_session"
CSRF_COOKIE_NAME = "lumenx_csrf"


def as_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite drops timezone metadata in tests."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SessionAuthenticationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle_seconds: int = 7 * 24 * 60 * 60
    absolute_seconds: int = 30 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.idle_seconds <= 0 or self.absolute_seconds < self.idle_seconds:
            raise ValueError("会话有效期配置不正确")


@dataclass(frozen=True, slots=True)
class IssuedSession:
    record: AuthSessionRecord
    token: str
    csrf_token: str
    policy: SessionPolicy


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    user_id: int
    session_id: int
    phone_canonical: str
    phone_verified: bool
    username: str | None = None


def hash_session_secret(value: str, session_secret: str) -> str:
    return hmac.new(
        session_secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_session(
    user_id: int,
    session_secret: str,
    *,
    policy: SessionPolicy | None = None,
    now: datetime | None = None,
    network_fingerprint: str | None = None,
    user_agent: str | None = None,
    configuration_version_id: str | int | None = None,
) -> IssuedSession:
    if len(session_secret) < 32:
        raise ValueError("会话密钥至少需要 32 个字符")
    active_policy = policy or SessionPolicy()
    try:
        canonical_configuration_version_id = parse_optional_database_id(
            configuration_version_id,
            field="配置版本 ID",
        )
    except ValueError as exc:
        raise ValueError("会话配置版本标识无效") from exc
    issued_at = now or datetime.now(UTC)
    token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    record = AuthSessionRecord(
        user_id=user_id,
        token_hash=hash_session_secret(token, session_secret),
        csrf_token_hash=hash_session_secret(csrf_token, session_secret),
        created_at=issued_at,
        last_seen_at=issued_at,
        idle_expires_at=issued_at + timedelta(seconds=active_policy.idle_seconds),
        absolute_expires_at=issued_at + timedelta(seconds=active_policy.absolute_seconds),
        idle_timeout_seconds=active_policy.idle_seconds,
        configuration_version_id=canonical_configuration_version_id,
        network_fingerprint=network_fingerprint,
        user_agent=user_agent[:512] if user_agent else None,
    )
    return IssuedSession(
        record=record,
        token=token,
        csrf_token=csrf_token,
        policy=active_policy,
    )


def set_session_cookies(
    response: Response,
    issued: IssuedSession,
    policy: SessionPolicy,
    *,
    secure: bool = True,
) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=policy.absolute_seconds,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=policy.absolute_seconds,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def clear_session_cookies(response: Response, *, secure: bool = True) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


class SessionService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        policy: SessionPolicy | None = None,
    ) -> None:
        if len(session_secret) < 32:
            raise ValueError("会话密钥至少需要 32 个字符")
        self.database = database
        self.session_secret = session_secret
        self.policy = policy or SessionPolicy()

    def create(
        self,
        user_id: int,
        *,
        network_fingerprint: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        issued = issue_session(
            user_id,
            self.session_secret,
            policy=self.policy,
            network_fingerprint=network_fingerprint,
            user_agent=user_agent,
        )
        identity = UserContext(user_id=str(user_id), session_id="pending")
        with self.database.transaction(identity) as session:
            session.add(issued.record)
            session.flush()
        return issued

    def resolve(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        now: datetime | None = None,
    ) -> SessionPrincipal:
        checked_at = now or datetime.now(UTC)
        token_hash = hash_session_secret(token, self.session_secret)
        failure: SessionAuthenticationError | None = None
        principal: SessionPrincipal | None = None

        with self.database.transaction() as session:
            set_transaction_session_token_hash(session, token_hash)
            record = session.scalar(
                select(AuthSessionRecord).where(AuthSessionRecord.token_hash == token_hash).limit(1)
            )
            if record is None or record.revoked_at is not None:
                failure = SessionAuthenticationError("AUTH_REQUIRED", "登录状态已失效，请重新登录")
            else:
                identity = UserContext(user_id=str(record.user_id), session_id=str(record.id))
                set_transaction_user_context(session, identity)
                user = session.get(UserRecord, record.user_id)
                if user is None:
                    failure = SessionAuthenticationError(
                        "AUTH_REQUIRED", "登录状态已失效，请重新登录"
                    )
                elif user.status != "active":
                    record.revoked_at = checked_at
                    failure = SessionAuthenticationError("ACCOUNT_SUSPENDED", "账号已停用")
                elif checked_at >= as_utc(record.idle_expires_at) or checked_at >= as_utc(
                    record.absolute_expires_at
                ):
                    record.revoked_at = checked_at
                    failure = SessionAuthenticationError(
                        "SESSION_EXPIRED", "登录已过期，请重新登录"
                    )
                elif csrf_token is not None and not hmac.compare_digest(
                    record.csrf_token_hash,
                    hash_session_secret(csrf_token, self.session_secret),
                ):
                    failure = SessionAuthenticationError(
                        "CSRF_INVALID", "安全校验失败，请刷新后重试"
                    )
                else:
                    record.last_seen_at = checked_at
                    idle_seconds = record.idle_timeout_seconds or self.policy.idle_seconds
                    record.idle_expires_at = min(
                        checked_at + timedelta(seconds=idle_seconds),
                        as_utc(record.absolute_expires_at),
                    )
                    principal = SessionPrincipal(
                        user_id=user.id,
                        session_id=record.id,
                        phone_canonical=user.phone_canonical,
                        phone_verified=user.phone_verified_at is not None,
                        username=user.username,
                    )

        if failure is not None:
            raise failure
        if principal is None:
            raise SessionAuthenticationError("AUTH_REQUIRED", "登录状态已失效，请重新登录")
        return principal

    def logout(self, token: str, *, now: datetime | None = None) -> None:
        revoked_at = now or datetime.now(UTC)
        token_hash = hash_session_secret(token, self.session_secret)
        with self.database.transaction() as session:
            set_transaction_session_token_hash(session, token_hash)
            record = session.scalar(
                select(AuthSessionRecord).where(AuthSessionRecord.token_hash == token_hash).limit(1)
            )
            if record is None:
                return
            set_transaction_user_context(
                session,
                UserContext(user_id=str(record.user_id), session_id=str(record.id)),
            )
            if record.revoked_at is None:
                record.revoked_at = revoked_at

    def revoke_all(
        self,
        identity: UserContext,
        *,
        target_user_id: int | None = None,
        now: datetime | None = None,
    ) -> int:
        identity_user_id = parse_database_id(identity.user_id, field="用户 ID")
        user_id = target_user_id or identity_user_id
        if user_id != identity_user_id:
            raise PermissionError("无权撤销其他用户的会话")
        revoked_at = now or datetime.now(UTC)
        with self.database.transaction(identity) as session:
            result = session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.user_id == user_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=revoked_at)
            )
            return int(result.rowcount or 0)

    def list_sessions(self, identity: UserContext) -> list[AuthSessionRecord]:
        with self.database.transaction(identity) as session:
            return list(
                session.scalars(
                    select(AuthSessionRecord)
                    .where(
                        AuthSessionRecord.user_id
                        == parse_database_id(identity.user_id, field="用户 ID")
                    )
                    .order_by(AuthSessionRecord.created_at.desc())
                )
            )

    def revoke_session(
        self,
        identity: UserContext,
        session_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.id == session_id,
                    AuthSessionRecord.user_id
                    == parse_database_id(identity.user_id, field="用户 ID"),
                )
            )
            if record is None:
                return False
            if record.revoked_at is None:
                record.revoked_at = now or datetime.now(UTC)
            return True
