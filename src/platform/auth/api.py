from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis import Redis

from ..contracts import UserContext
from ..audit import AuditService
from ..error_protocol import request_correlation_id
from ..database import Database
from ..feature_flags import CloudFeatureDisabledError, CloudFeatureGate
from ..observability import events, metrics
from ..runtime_policy import RuntimePolicyResolver, RuntimePolicyUnavailableError
from ..settings import DeploymentSettings
from .admin import (
    AdminAuthorizationError,
    ResetCredentialError,
    SupportPasswordResetService,
    UserAdministrationService,
    UserNotFoundError,
)
from .protection import AuthRateLimiter, RateLimitExceeded, RateLimitPolicy, UNSAFE_METHODS
from .registration import RegistrationPolicy, RegistrationService
from .invitations import InvitationError, InvitationService
from .security import DuplicatePhoneError, InvalidPhoneError, PasswordPolicyError, PasswordService
from .service import (
    AccountSuspendedError,
    AuthenticationPolicy,
    AuthenticationService,
    CredentialError,
)
from .sessions import (
    SessionAuthenticationError,
    SessionPolicy,
    SessionPrincipal,
    SessionService,
    clear_session_cookies,
    set_session_cookies,
)


class RegisterRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)
    invitation_code: str | None = Field(default=None, min_length=1, max_length=256)


class LoginRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class UserProfileResponse(BaseModel):
    id: int
    phone: str
    phone_verified: bool
    phone_verification_status: str
    is_platform_admin: bool
    default_workspace_id: int


class AuthResponse(BaseModel):
    user: UserProfileResponse


class MessageResponse(BaseModel):
    message: str


class SessionResponse(BaseModel):
    id: int
    current: bool
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    user_agent: str | None


class AdminReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class SupportResetRequest(BaseModel):
    credential: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


class ResetCredentialResponse(BaseModel):
    credential: str
    expires_at: datetime


class RegistrationPolicyResponse(BaseModel):
    mode: str
    verification_available: bool


class InvitationCreateRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    expires_at: datetime
    reason: str = Field(min_length=1, max_length=1000)


class InvitationResponse(BaseModel):
    id: int
    phone: str
    status: str
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    issue_reason: str
    revoke_reason: str | None
    invitation_code: str | None = None


@dataclass(slots=True)
class AuthApplication:
    database: Database
    redis: Redis
    registration: RegistrationService
    authentication: AuthenticationService
    sessions: SessionService
    user_administration: UserAdministrationService
    support_reset: SupportPasswordResetService
    rate_limiter: AuthRateLimiter
    registration_rate: RateLimitPolicy
    login_rate: RateLimitPolicy
    reset_rate: RateLimitPolicy
    feature_gate: CloudFeatureGate | None = None
    invitations: InvitationService | None = None
    verification_available: bool = False
    runtime_policy: RuntimePolicyResolver | None = None


def _network_source(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _profile(
    *,
    user_id: int,
    phone: str,
    phone_verified: bool,
    is_platform_admin: bool,
    workspace_id: int,
) -> UserProfileResponse:
    return UserProfileResponse(
        id=user_id,
        phone=phone,
        phone_verified=phone_verified,
        phone_verification_status="已验证" if phone_verified else "未验证",
        is_platform_admin=is_platform_admin,
        default_workspace_id=workspace_id,
    )


def create_auth_router(application: AuthApplication) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["用户认证"])
    audit = AuditService(application.database)

    def require_principal(request: Request) -> SessionPrincipal:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token") if request.method in UNSAFE_METHODS else None
        )
        return application.sessions.resolve(token, csrf_token=csrf_token)

    def require_admin(
        principal: SessionPrincipal = Depends(require_principal),
    ) -> SessionPrincipal:
        if not principal.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以执行此操作")
        return principal

    def admin_identity(principal: SessionPrincipal) -> UserContext:
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=True,
        )

    def auth_started(request: Request, action: str) -> None:
        request.state.auth_action = action
        request.state.auth_started_at = time.perf_counter()

    def auth_succeeded(request: Request, action: str, user_id: int) -> None:
        metrics.increment(
            "auth_results_total",
            labels={"action": action, "outcome": "succeeded"},
        )
        metrics.observe(
            "auth_request_duration_seconds",
            max(time.perf_counter() - request.state.auth_started_at, 0),
            labels={"action": action, "outcome": "succeeded"},
        )
        events.emit(
            "auth.succeeded",
            action=action,
            user_id=str(user_id),
            correlation_id=request_correlation_id(request),
        )

    def invitation_response(record, *, plaintext_code: str | None = None) -> InvitationResponse:
        return InvitationResponse(
            id=record.id,
            phone=record.phone_canonical,
            status=record.status,
            expires_at=record.expires_at,
            created_at=record.created_at,
            consumed_at=record.consumed_at,
            revoked_at=record.revoked_at,
            issue_reason=record.issue_reason,
            revoke_reason=record.revoke_reason,
            invitation_code=plaintext_code,
        )

    @router.get("/registration-policy", response_model=RegistrationPolicyResponse)
    def registration_policy() -> RegistrationPolicyResponse:
        if application.feature_gate is None:
            return RegistrationPolicyResponse(
                mode="disabled",
                verification_available=False,
            )
        state = application.feature_gate.state()
        return RegistrationPolicyResponse(
            mode=state.registration_mode.value,
            verification_available=application.verification_available,
        )

    @router.post("/register", response_model=AuthResponse, status_code=201)
    def register(payload: RegisterRequest, request: Request, response: Response) -> AuthResponse:
        auth_started(request, "registration")
        registration_state = (
            application.feature_gate.require_registration()
            if application.feature_gate is not None
            else None
        )
        network = _network_source(request)
        application.rate_limiter.check(
            "registration",
            application.registration_rate,
            phone_canonical=payload.phone.strip(),
            network_source=network,
        )
        try:
            result = application.registration.register(
                payload.phone,
                payload.password,
                network_fingerprint=application.rate_limiter.fingerprint(network),
                user_agent=request.headers.get("user-agent"),
                invitation_code=payload.invitation_code,
            )
        except DuplicatePhoneError as exc:
            if registration_state is not None and registration_state.registration_mode.value == "invite_only":
                raise InvitationError("INVITATION_INVALID") from exc
            raise
        set_session_cookies(response, result.session, result.session.policy)
        audit.record(
            UserContext(user_id=str(result.user_id), session_id=str(result.session.record.id)),
            action="auth.register",
            target_type="user",
            target_id=str(result.user_id),
            request=request,
            workspace_id=str(result.workspace_id),
            after={"phone_verified": result.phone_verified, "session_created": True},
        )
        auth_succeeded(request, "registration", result.user_id)
        return AuthResponse(
            user=_profile(
                user_id=result.user_id,
                phone=result.phone_canonical,
                phone_verified=result.phone_verified,
                is_platform_admin=False,
                workspace_id=result.workspace_id,
            )
        )

    @router.post("/login", response_model=AuthResponse)
    def login(payload: LoginRequest, request: Request, response: Response) -> AuthResponse:
        auth_started(request, "login")
        network = _network_source(request)
        application.rate_limiter.check(
            "login",
            application.login_rate,
            phone_canonical=payload.phone.strip(),
            network_source=network,
        )
        result = application.authentication.login(
            payload.phone,
            payload.password,
            network_fingerprint=application.rate_limiter.fingerprint(network),
            user_agent=request.headers.get("user-agent"),
        )
        set_session_cookies(response, result.session, result.session.policy)
        audit.record(
            UserContext(
                user_id=str(result.user_id),
                session_id=str(result.session.record.id),
                is_platform_admin=result.is_platform_admin,
            ),
            action="auth.login",
            target_type="session",
            target_id=str(result.session.record.id),
            request=request,
            workspace_id=str(result.workspace_id),
            after={"session_created": True},
        )
        auth_succeeded(request, "login", result.user_id)
        return AuthResponse(
            user=_profile(
                user_id=result.user_id,
                phone=result.phone_canonical,
                phone_verified=result.phone_verified,
                is_platform_admin=result.is_platform_admin,
                workspace_id=result.workspace_id,
            )
        )

    @router.post("/logout", response_model=MessageResponse)
    def logout(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> MessageResponse:
        token = request.cookies.get("lumenx_session")
        if token:
            application.sessions.logout(token)
        audit.record(
            UserContext(user_id=str(principal.user_id), session_id=str(principal.session_id)),
            action="auth.logout",
            target_type="session",
            target_id=str(principal.session_id),
            request=request,
            after={"revoked": True},
        )
        clear_session_cookies(response)
        return MessageResponse(message="已退出登录")

    @router.get("/me", response_model=AuthResponse)
    def current_user(principal: SessionPrincipal = Depends(require_principal)) -> AuthResponse:
        workspace_id = application.authentication.default_workspace_id(principal)
        return AuthResponse(
            user=_profile(
                user_id=principal.user_id,
                phone=principal.phone_canonical,
                phone_verified=principal.phone_verified,
                is_platform_admin=principal.is_platform_admin,
                workspace_id=workspace_id,
            )
        )

    @router.post("/password", response_model=MessageResponse)
    def change_password(
        payload: ChangePasswordRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> MessageResponse:
        application.authentication.change_password(
            principal,
            payload.current_password,
            payload.new_password,
        )
        audit.record(
            UserContext(user_id=str(principal.user_id), session_id=str(principal.session_id)),
            action="auth.password.change",
            target_type="user",
            target_id=str(principal.user_id),
            request=request,
            after={"other_sessions_revoked": True},
        )
        return MessageResponse(message="密码已更新，其他设备已退出登录")

    @router.get("/sessions", response_model=list[SessionResponse])
    def list_sessions(
        principal: SessionPrincipal = Depends(require_principal),
    ) -> list[SessionResponse]:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=principal.is_platform_admin,
        )
        return [
            SessionResponse(
                id=record.id,
                current=record.id == principal.session_id,
                created_at=record.created_at,
                last_seen_at=record.last_seen_at,
                idle_expires_at=record.idle_expires_at,
                absolute_expires_at=record.absolute_expires_at,
                revoked_at=record.revoked_at,
                user_agent=record.user_agent,
            )
            for record in application.sessions.list_sessions(identity)
        ]

    @router.delete("/sessions/{session_id}", response_model=MessageResponse)
    def revoke_session(
        session_id: int,
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> MessageResponse:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=principal.is_platform_admin,
        )
        if not application.sessions.revoke_session(identity, session_id):
            return MessageResponse(message="会话不存在或已失效")
        if session_id == principal.session_id:
            clear_session_cookies(response)
        audit.record(
            identity,
            action="auth.session.revoke",
            target_type="session",
            target_id=str(session_id),
            request=request,
            after={"revoked": True},
        )
        return MessageResponse(message="会话已撤销")

    @router.post("/sessions/revoke-all", response_model=MessageResponse)
    def revoke_all_sessions(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> MessageResponse:
        identity = UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=principal.is_platform_admin,
        )
        application.sessions.revoke_all(identity)
        audit.record(
            identity,
            action="auth.sessions.revoke_all",
            target_type="user",
            target_id=str(principal.user_id),
            request=request,
            after={"all_sessions_revoked": True},
        )
        clear_session_cookies(response)
        return MessageResponse(message="所有设备已退出登录")

    @router.post("/support-reset", response_model=MessageResponse)
    def support_reset(payload: SupportResetRequest, request: Request) -> MessageResponse:
        auth_started(request, "support-reset")
        network = _network_source(request)
        application.rate_limiter.check(
            "support-reset",
            application.reset_rate,
            phone_canonical=payload.credential,
            network_source=network,
        )
        application.support_reset.reset_password(
            payload.credential,
            payload.new_password,
        )
        metrics.increment(
            "auth_results_total",
            labels={"action": "support-reset", "outcome": "succeeded"},
        )
        return MessageResponse(message="密码已重置，请使用新密码登录")

    @router.post(
        "/admin/users/{user_id}/suspend",
        response_model=MessageResponse,
        tags=["用户管理"],
    )
    def suspend_user(
        user_id: int,
        payload: AdminReasonRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> MessageResponse:
        application.user_administration.set_status(
            admin_identity(principal), user_id, "suspended", payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return MessageResponse(message="用户已停用，所有会话已撤销")

    @router.post(
        "/admin/users/{user_id}/reactivate",
        response_model=MessageResponse,
        tags=["用户管理"],
    )
    def reactivate_user(
        user_id: int,
        payload: AdminReasonRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> MessageResponse:
        application.user_administration.set_status(
            admin_identity(principal), user_id, "active", payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return MessageResponse(message="用户已恢复")

    @router.post(
        "/admin/users/{user_id}/revoke-sessions",
        response_model=MessageResponse,
        tags=["用户管理"],
    )
    def admin_revoke_sessions(
        user_id: int,
        payload: AdminReasonRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> MessageResponse:
        count = application.user_administration.revoke_sessions(
            admin_identity(principal), user_id, payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return MessageResponse(message=f"已撤销 {count} 个会话")

    @router.post(
        "/admin/users/{user_id}/reset-credentials",
        response_model=ResetCredentialResponse,
        tags=["用户管理"],
    )
    def issue_reset_credential(
        user_id: int,
        payload: AdminReasonRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> ResetCredentialResponse:
        issued = application.user_administration.issue_reset_credential(
            admin_identity(principal), user_id, payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return ResetCredentialResponse(
            credential=issued.credential,
            expires_at=issued.expires_at,
        )

    @router.get(
        "/admin/invitations",
        response_model=list[InvitationResponse],
        tags=["注册邀请管理"],
    )
    def list_invitations(
        principal: SessionPrincipal = Depends(require_admin),
    ) -> list[InvitationResponse]:
        if application.invitations is None:
            raise RuntimeError("邀请注册服务尚未就绪")
        return [
            invitation_response(record)
            for record in application.invitations.list(admin_identity(principal))
        ]

    @router.post(
        "/admin/invitations",
        response_model=InvitationResponse,
        status_code=201,
        tags=["注册邀请管理"],
    )
    def create_invitation(
        payload: InvitationCreateRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> InvitationResponse:
        if application.invitations is None:
            raise RuntimeError("邀请注册服务尚未就绪")
        issued = application.invitations.issue(
            admin_identity(principal),
            phone=payload.phone,
            expires_at=payload.expires_at,
            reason=payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return invitation_response(
            issued.record,
            plaintext_code=issued.plaintext_code,
        )

    @router.post(
        "/admin/invitations/{invitation_id}/revoke",
        response_model=InvitationResponse,
        tags=["注册邀请管理"],
    )
    def revoke_invitation(
        invitation_id: int,
        payload: AdminReasonRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_admin),
    ) -> InvitationResponse:
        if application.invitations is None:
            raise RuntimeError("邀请注册服务尚未就绪")
        record = application.invitations.revoke(
            admin_identity(principal),
            invitation_id,
            reason=payload.reason,
            correlation_id=request_correlation_id(request),
        )
        return invitation_response(record)

    return router


def install_cloud_auth(app: FastAPI, settings: DeploymentSettings) -> AuthApplication:
    session_secret = settings.session_secret.get_secret_value()
    session_policy = SessionPolicy(
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    database = Database(settings.database_url)
    redis_client = Redis.from_url(settings.redis_url)
    password_service = PasswordService()
    runtime_policy = RuntimePolicyResolver(database, verification_available=False)
    invitations = InvitationService(database, session_secret)

    def registration_policy_from_database() -> RegistrationPolicy:
        active = runtime_policy.resolve()
        return RegistrationPolicy(
            initial_grant_microtickets=active.registration_initial_grant_microtickets,
            session_policy=active.session_policy,
            registration_mode=active.registration_mode,
            config_version_id=active.config_version_id,
            max_sessions_per_user=active.max_sessions_per_user,
        )

    def authentication_policy_from_database() -> AuthenticationPolicy:
        active = runtime_policy.resolve()
        return AuthenticationPolicy(
            session_policy=active.session_policy,
            max_sessions_per_user=active.max_sessions_per_user,
            config_version_id=active.config_version_id,
        )
    sessions = SessionService(database, session_secret, policy=session_policy)
    feature_gate = CloudFeatureGate(
        database,
        registration_emergency_disabled=settings.registration_emergency_disabled,
        new_ai_tasks_emergency_disabled=settings.new_ai_tasks_emergency_disabled,
    )
    application = AuthApplication(
        database=database,
        redis=redis_client,
        registration=RegistrationService(
            database,
            session_secret,
            password_service=password_service,
            policy_resolver=registration_policy_from_database,
            invitations=invitations,
        ),
        authentication=AuthenticationService(
            database,
            session_secret,
            password_service=password_service,
            session_policy=session_policy,
            policy_resolver=authentication_policy_from_database,
        ),
        sessions=sessions,
        user_administration=UserAdministrationService(
            database,
            session_secret,
            password_service=password_service,
        ),
        support_reset=SupportPasswordResetService(
            database,
            session_secret,
            password_service=password_service,
        ),
        rate_limiter=AuthRateLimiter(redis_client, session_secret),
        registration_rate=RateLimitPolicy(
            settings.auth_registration_limit,
            settings.auth_rate_window_seconds,
        ),
        login_rate=RateLimitPolicy(
            settings.auth_login_limit,
            settings.auth_rate_window_seconds,
        ),
        reset_rate=RateLimitPolicy(
            settings.auth_reset_limit,
            settings.auth_rate_window_seconds,
        ),
        feature_gate=feature_gate,
        invitations=invitations,
        verification_available=False,
        runtime_policy=runtime_policy,
    )
    app.state.auth_application = application
    app.state.database = database
    app.include_router(create_auth_router(application))

    def record_auth_failure(request: Request, error_code: str) -> None:
        action = getattr(request.state, "auth_action", "session")
        metrics.increment(
            "auth_results_total",
            labels={"action": action, "outcome": "failed"},
        )
        started_at = getattr(request.state, "auth_started_at", None)
        if isinstance(started_at, float):
            metrics.observe(
                "auth_request_duration_seconds",
                max(time.perf_counter() - started_at, 0),
                labels={"action": action, "outcome": "failed"},
            )
        events.emit(
            "auth.failed",
            action=action,
            error_code=error_code,
            correlation_id=request_correlation_id(request),
        )

    @app.exception_handler(SessionAuthenticationError)
    def handle_session_error(request: Request, exc: SessionAuthenticationError):
        record_auth_failure(request, exc.code)
        response = JSONResponse(
            status_code=403 if exc.code in {"ACCOUNT_SUSPENDED", "CSRF_INVALID"} else 401,
            content={"code": exc.code, "message": str(exc)},
        )
        clear_session_cookies(response)
        return response

    @app.exception_handler(CredentialError)
    def handle_credential_error(request: Request, exc: CredentialError):
        record_auth_failure(request, "INVALID_CREDENTIALS")
        return JSONResponse(
            status_code=401,
            content={"code": "INVALID_CREDENTIALS", "message": str(exc)},
        )

    @app.exception_handler(AccountSuspendedError)
    def handle_suspended_error(request: Request, exc: AccountSuspendedError):
        record_auth_failure(request, "ACCOUNT_SUSPENDED")
        return JSONResponse(
            status_code=403,
            content={"code": "ACCOUNT_SUSPENDED", "message": str(exc)},
        )

    @app.exception_handler(RateLimitExceeded)
    def handle_rate_limit(request: Request, exc: RateLimitExceeded):
        record_auth_failure(request, "RATE_LIMITED")
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after_seconds)},
            content={"code": "RATE_LIMITED", "message": str(exc)},
        )

    @app.exception_handler(CloudFeatureDisabledError)
    def handle_feature_disabled(_request: Request, exc: CloudFeatureDisabledError):
        return JSONResponse(
            status_code=503,
            content={"code": exc.code, "message": str(exc)},
        )

    @app.exception_handler(DuplicatePhoneError)
    def handle_duplicate_phone(request: Request, exc: DuplicatePhoneError):
        record_auth_failure(request, "PHONE_ALREADY_REGISTERED")
        return JSONResponse(
            status_code=409,
            content={"code": "PHONE_ALREADY_REGISTERED", "message": str(exc)},
        )

    @app.exception_handler(InvitationError)
    def handle_invitation_error(request: Request, exc: InvitationError):
        record_auth_failure(request, "INVITATION_INVALID")
        events.emit(
            "registration.invitation_rejected",
            error_code=exc.code,
            correlation_id=request_correlation_id(request),
        )
        return JSONResponse(
            status_code=409,
            content={"code": "INVITATION_INVALID", "message": str(exc)},
        )

    @app.exception_handler(RuntimePolicyUnavailableError)
    def handle_policy_unavailable(_request: Request, exc: RuntimePolicyUnavailableError):
        return JSONResponse(
            status_code=503,
            content={"code": "RUNTIME_POLICY_UNAVAILABLE", "message": str(exc)},
        )

    def handle_validation_error(request: Request, exc: ValueError):
        record_auth_failure(request, "VALIDATION_ERROR")
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": str(exc)},
        )

    app.add_exception_handler(InvalidPhoneError, handle_validation_error)
    app.add_exception_handler(PasswordPolicyError, handle_validation_error)

    @app.exception_handler(AdminAuthorizationError)
    def handle_admin_denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(
            status_code=403,
            content={"code": "ADMIN_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(UserNotFoundError)
    def handle_user_not_found(_request: Request, exc: UserNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "USER_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(ResetCredentialError)
    def handle_reset_error(_request: Request, exc: ResetCredentialError):
        return JSONResponse(
            status_code=400,
            content={"code": "RESET_CREDENTIAL_INVALID", "message": str(exc)},
        )

    def close_auth_resources() -> None:
        redis_client.close()
        database.dispose()

    app.router.add_event_handler("shutdown", close_auth_resources)
    return application
