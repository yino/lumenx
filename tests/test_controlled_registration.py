from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.platform.auth.invitations import InvitationError, InvitationService
from src.platform.auth.registration import RegistrationPolicy, RegistrationService
from src.platform.configuration_schemas import RegistrationMode
from src.platform.contracts import AdminContext
from src.platform.db_models import (
    AuditEventRecord,
    Base,
    RegistrationInvitationRecord,
    TicketLedgerRecord,
    UserRecord,
)
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def invitation_environment():
    database = RepositoryDatabase()
    Base.metadata.create_all(database.engine, checkfirst=True)
    context = _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    invitations = InvitationService(database, "i" * 32)
    yield database, admin, invitations
    database.engine.dispose()


def _issue(invitations: InvitationService, admin, phone: str = "13800138001"):
    return invitations.issue(
        admin,
        phone=phone,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        reason="邀请内测创作者",
    )


def _registration(database, invitations: InvitationService) -> RegistrationService:
    return RegistrationService(
        database,
        "s" * 32,
        policy=RegistrationPolicy(
            initial_grant_microtickets=2_000_000,
            registration_mode=RegistrationMode.INVITE_ONLY,
            config_version_id=None,
        ),
        invitations=invitations,
    )


def test_invitation_registration_is_atomic_and_plaintext_is_not_persisted(
    invitation_environment,
) -> None:
    database, admin, invitations = invitation_environment
    issued = _issue(invitations, admin)

    result = _registration(database, invitations).register(
        "13800138001",
        "secure-pass-2026",
        invitation_code=issued.plaintext_code,
    )

    with database.session_factory() as session:
        record = session.get(RegistrationInvitationRecord, issued.record.id)
        ledger = session.scalar(
            select(TicketLedgerRecord).where(
                TicketLedgerRecord.user_id == result.user_id,
                TicketLedgerRecord.entry_type == "grant",
            )
        )
        assert record.status == "consumed"
        assert record.consumed_by_user_id == result.user_id
        assert issued.plaintext_code not in str(record.__dict__)
        assert ledger.correlation["registration_mode"] == "invite_only"
        assert ledger.correlation["grant_microtickets"] == 2_000_000


def test_invitation_phone_mismatch_rolls_back_without_disclosure(
    invitation_environment,
) -> None:
    database, admin, invitations = invitation_environment
    issued = _issue(invitations, admin)

    with pytest.raises(InvitationError, match="无效或已失效"):
        _registration(database, invitations).register(
            "13900139001",
            "secure-pass-2026",
            invitation_code=issued.plaintext_code,
        )

    with database.session_factory() as session:
        record = session.get(RegistrationInvitationRecord, issued.record.id)
        assert record.status == "active"
        assert session.scalar(
            select(func.count(UserRecord.id)).where(
                UserRecord.phone_canonical == "+8613900139001"
            )
        ) == 0


def test_invitation_replay_revocation_and_expiry_are_rejected(
    invitation_environment,
) -> None:
    database, admin, invitations = invitation_environment
    replayed = _issue(invitations, admin, "13800138002")
    service = _registration(database, invitations)
    service.register(
        "13800138002",
        "secure-pass-2026",
        invitation_code=replayed.plaintext_code,
    )
    with pytest.raises(InvitationError):
        service.register(
            "13800138002",
            "secure-pass-2026",
            invitation_code=replayed.plaintext_code,
        )

    revoked = _issue(invitations, admin, "13800138003")
    invitations.revoke(admin, revoked.record.id, reason="用户不再参与内测")
    with pytest.raises(InvitationError):
        service.register(
            "13800138003",
            "secure-pass-2026",
            invitation_code=revoked.plaintext_code,
        )

    expired = invitations.issue(
        admin,
        phone="13800138004",
        expires_at=datetime.now(UTC) + timedelta(seconds=1),
        reason="短期邀请",
        now=datetime.now(UTC),
    )
    with database.session_factory.begin() as session:
        session.get(RegistrationInvitationRecord, expired.record.id).expires_at = (
            datetime.now(UTC) - timedelta(seconds=1)
        )
    with pytest.raises(InvitationError):
        service.register(
            "13800138004",
            "secure-pass-2026",
            invitation_code=expired.plaintext_code,
        )

    with database.session_factory() as session:
        actions = set(session.scalars(select(AuditEventRecord.action)))
        assert "registration.invitation.create" in actions
        assert "registration.invitation.revoke" in actions
