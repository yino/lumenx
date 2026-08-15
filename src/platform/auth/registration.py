from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy.exc import IntegrityError

from ..contracts import UserContext
from ..database import (
    Database,
    set_transaction_registration_phone,
    set_transaction_user_context,
)
from ..db_models import AuditEventRecord, UserRecord, WorkspaceRecord
from ..configuration_schemas import RegistrationMode
from ..ticket_wallet import TicketWalletService
from .invitations import InvitationService
from .security import DuplicatePhoneError, PasswordService, normalize_phone
from .sessions import IssuedSession, SessionPolicy, issue_session


@dataclass(frozen=True, slots=True)
class RegistrationPolicy:
    initial_grant_microtickets: int = 0
    default_workspace_name: str = "默认工作区"
    session_policy: SessionPolicy = SessionPolicy()
    registration_mode: RegistrationMode = RegistrationMode.DISABLED
    config_version_id: str | None = None
    max_sessions_per_user: int = 1

    def __post_init__(self) -> None:
        if self.initial_grant_microtickets < 0:
            raise ValueError("注册初始赠送算力券不能为负数")
        if not self.default_workspace_name.strip():
            raise ValueError("默认工作区名称不能为空")
        if self.max_sessions_per_user < 1:
            raise ValueError("单用户会话上限必须大于零")


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user_id: int
    workspace_id: int
    phone_canonical: str
    phone_verified: bool
    session: IssuedSession


class RegistrationService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        password_service: PasswordService | None = None,
        policy: RegistrationPolicy | None = None,
        policy_resolver: Callable[[], RegistrationPolicy] | None = None,
        invitations: InvitationService | None = None,
    ) -> None:
        self.database = database
        self.session_secret = session_secret
        self.password_service = password_service or PasswordService()
        self.policy = policy or RegistrationPolicy()
        self.policy_resolver = policy_resolver
        self.invitations = invitations

    def register(
        self,
        phone: str,
        password: str,
        *,
        network_fingerprint: str | None = None,
        user_agent: str | None = None,
        invitation_code: str | None = None,
    ) -> RegistrationResult:
        policy = self.policy_resolver() if self.policy_resolver else self.policy
        if policy.registration_mode is RegistrationMode.DISABLED:
            raise PermissionError("注册暂未开放，请稍后再试")
        if (
            policy.registration_mode is RegistrationMode.INVITE_ONLY
            and self.invitations is None
        ):
            raise RuntimeError("邀请注册服务尚未就绪")
        phone_canonical = normalize_phone(phone)
        password_hash = self.password_service.hash(password)
        user_id: int
        workspace_id: int
        issued_session: IssuedSession

        try:
            with self.database.transaction() as session:
                invitation = None
                registered_at = datetime.now(UTC)
                if policy.registration_mode is RegistrationMode.INVITE_ONLY:
                    invitation = self.invitations.require_for_registration(
                        session,
                        phone_canonical=phone_canonical,
                        plaintext_code=invitation_code,
                        now=registered_at,
                    )
                set_transaction_registration_phone(session, phone_canonical)
                user = UserRecord(
                    phone_canonical=phone_canonical,
                    password_hash=password_hash,
                    status="active",
                    phone_verified_at=None,
                    is_platform_admin=False,
                )
                session.add(user)
                session.flush()
                user_id = user.id
                identity = UserContext(
                    user_id=str(user_id),
                    session_id="registration-pending",
                )
                set_transaction_user_context(session, identity)

                issued_session = issue_session(
                    user_id,
                    self.session_secret,
                    policy=policy.session_policy,
                    network_fingerprint=network_fingerprint,
                    user_agent=user_agent,
                    configuration_version_id=policy.config_version_id,
                )

                TicketWalletService.create_wallet_in_session(
                    session,
                    user_id,
                    policy.initial_grant_microtickets,
                    reason="注册初始赠送",
                    correlation={
                        "source": "registration",
                        "config_version_id": policy.config_version_id,
                        "grant_microtickets": policy.initial_grant_microtickets,
                        "registration_mode": policy.registration_mode.value,
                        "invitation_id": str(invitation.id) if invitation else None,
                    },
                )
                workspace = WorkspaceRecord(
                    user_id=user_id,
                    name=policy.default_workspace_name.strip(),
                )
                session.add_all([workspace, issued_session.record])
                session.flush()
                workspace_id = workspace.id
                if invitation is not None:
                    self.invitations.consume(
                        invitation,
                        user_id=user_id,
                        consumed_at=registered_at,
                    )
                    session.add(
                        AuditEventRecord(
                            actor_user_id=user_id,
                            target_user_id=user_id,
                            action="registration.invitation.consume",
                            target_type="registration_invitation",
                            target_id=str(invitation.id),
                            reason="用户完成邀请注册",
                            before_summary={"status": "active"},
                            after_summary={
                                "status": "consumed",
                                "config_version_id": policy.config_version_id,
                            },
                            correlation_id=str(uuid.uuid4()),
                        )
                    )
        except IntegrityError as exc:
            constraint_name = getattr(getattr(exc, "orig", None), "diag", None)
            if getattr(constraint_name, "constraint_name", None) == "uq_users_phone_canonical":
                raise DuplicatePhoneError("该手机号已注册") from exc
            raise

        return RegistrationResult(
            user_id=user_id,
            workspace_id=workspace_id,
            phone_canonical=phone_canonical,
            phone_verified=False,
            session=issued_session,
        )
