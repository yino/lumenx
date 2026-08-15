from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from src.platform.auth.admin import (
    AdminAuthorizationError,
    ResetCredentialError,
    SupportPasswordResetService,
    UserAdministrationService,
)
from src.platform.auth.security import PasswordService
from src.platform.contracts import UserContext
from src.platform.db_models import (
    AuditEventRecord,
    PasswordResetCredentialRecord,
    UserRecord,
)


class AdministrationDatabase:
    def __init__(self, session: Mock) -> None:
        self.session = session

    @contextmanager
    def transaction(self, _identity=None):
        yield self.session


def admin_context() -> UserContext:
    return UserContext(user_id="1", is_platform_admin=True)


def test_non_admin_cannot_change_user_status() -> None:
    service = UserAdministrationService(AdministrationDatabase(Mock()), "s" * 32)

    with pytest.raises(AdminAuthorizationError):
        service.set_status(
            UserContext(user_id="2"),
            3,
            "suspended",
            "安全处置",
        )


def test_non_admin_cannot_create_user() -> None:
    service = UserAdministrationService(AdministrationDatabase(Mock()), "s" * 32)

    with pytest.raises(AdminAuthorizationError):
        service.create_user(
            UserContext(user_id="2"),
            "13800138000",
            "secure-pass-2026",
            "后台开户",
        )


def test_suspension_revokes_sessions_and_appends_audit() -> None:
    target = UserRecord(
        id=2,
        phone_canonical="+8613800138000",
        password_hash="hash",
        status="active",
        is_platform_admin=False,
    )
    session = Mock()
    session.get.return_value = target
    service = UserAdministrationService(AdministrationDatabase(session), "s" * 32)

    service.set_status(admin_context(), target.id, "suspended", "发现账号风险")

    assert target.status == "suspended"
    session.execute.assert_called_once()
    audit = session.add.call_args.args[0]
    assert isinstance(audit, AuditEventRecord)
    assert audit.reason == "发现账号风险"
    assert audit.before_summary == {"status": "active"}
    assert "token" not in str(audit.after_summary).lower()


def test_reset_credential_is_single_use_and_revokes_sessions() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    user = UserRecord(
        id=2,
        phone_canonical="+8613800138000",
        password_hash="old-hash",
        status="active",
        is_platform_admin=False,
    )
    record = PasswordResetCredentialRecord(
        id=3,
        user_id=user.id,
        created_by_admin_id=1,
        token_hash="stored-hash",
        reason="用户人工核验通过",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session = Mock()
    session.scalar.return_value = record
    session.get.return_value = user
    passwords = PasswordService()
    service = SupportPasswordResetService(
        AdministrationDatabase(session), "s" * 32, password_service=passwords
    )

    service.reset_password("support-reset-token", "new-secure-pass-2026", now=now)

    assert user.phone_verified_at is None
    assert record.used_at == now
    assert passwords.verify(user.password_hash, "new-secure-pass-2026") is True
    assert session.execute.call_count == 3

    with pytest.raises(ResetCredentialError):
        service.reset_password("support-reset-token", "another-pass-2026", now=now)
