from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.platform.auth.security import PasswordService
from src.platform.bootstrap_admin import (
    BootstrapAdminNotFoundError,
    BootstrapAlreadyCompletedError,
    BootstrapSoleAdminRecoveryError,
    InsecureBootstrapPasswordError,
    PlatformAdminBootstrapService,
)
from src.platform.auth.admin_identity import issue_admin_session
from src.platform.auth.sessions import SessionPolicy
from src.platform.db_models import (
    AdminSessionRecord,
    AdminUserRecord,
    AuditEventRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
    WorkspaceRecord,
)
from tests.test_content_repositories import RepositoryDatabase


def _database() -> RepositoryDatabase:
    database = RepositoryDatabase()
    AdminUserRecord.__table__.create(database.engine)
    AdminSessionRecord.__table__.create(database.engine)
    TicketWalletRecord.__table__.create(database.engine)
    TicketLedgerRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    return database


def test_bootstrap_creates_only_independent_admin_and_is_idempotent() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)

    first = service.bootstrap("admin", "SecurePass123")
    second = service.bootstrap("ADMIN", "DoNotReset123")

    assert first.created is True
    assert second.created is False
    assert second.admin_id == first.admin_id
    with database.session_factory() as session:
        admin = session.get(AdminUserRecord, int(first.admin_id))
        assert admin is not None
        assert PasswordService().verify(admin.password_hash, "SecurePass123") is True
        assert PasswordService().verify(admin.password_hash, "DoNotReset123") is False
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 0
        assert session.scalar(select(func.count()).select_from(WorkspaceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketWalletRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 0
        assert session.scalar(select(func.count()).select_from(AdminSessionRecord)) == 0
    database.engine.dispose()


def test_bootstrap_cannot_create_a_second_platform_admin() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)
    service.bootstrap("admin", "SecurePass123")

    with pytest.raises(BootstrapAlreadyCompletedError, match="不能通过初始化命令"):
        service.bootstrap("backup-admin", "AnotherSecurePass123")

    database.engine.dispose()


def test_local_default_password_requires_explicit_local_allowance() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)

    with pytest.raises(InsecureBootstrapPasswordError, match="非本地环境"):
        service.bootstrap("admin", "sk532359025")

    created = service.bootstrap(
        "admin",
        "sk532359025",
        allow_local_default_password=True,
    )
    with database.session_factory() as session:
        admin = session.get(AdminUserRecord, int(created.admin_id))
        assert admin is not None
        assert admin.username == "admin"
        assert admin.password_hash != "sk532359025"
        assert PasswordService().verify(admin.password_hash, "sk532359025") is True
        assert admin.must_change_password is True
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 1
    database.engine.dispose()


def test_existing_admin_password_is_preserved_on_rerun() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)
    created = service.bootstrap("admin", "InitialSecurePass123")

    repeated = service.bootstrap("ADMIN", "ReplacementMustNotApply123")

    assert repeated.admin_id == created.admin_id
    assert repeated.created is False
    with database.session_factory() as session:
        admin = session.get(AdminUserRecord, int(created.admin_id))
        assert PasswordService().verify(admin.password_hash, "InitialSecurePass123") is True
        assert PasswordService().verify(admin.password_hash, "ReplacementMustNotApply123") is False
        assert session.scalar(select(func.count(AdminUserRecord.id))) == 1
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 1
    database.engine.dispose()


def test_local_bootstrap_adopts_existing_sole_admin_once() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)
    created = service.bootstrap("legacy-admin", "InitialSecurePass123")
    issued = issue_admin_session(
        int(created.admin_id),
        "l" * 32,
        policy=SessionPolicy(),
    )
    with database.session_factory.begin() as session:
        session.add(issued.record)

    adopted = service.bootstrap(
        "ADMIN",
        "sk532359025",
        allow_local_default_password=True,
        adopt_existing_sole_admin=True,
    )
    repeated = service.bootstrap(
        "admin",
        "MustNotRotateAgain123",
        allow_local_default_password=True,
        adopt_existing_sole_admin=True,
    )

    assert adopted.admin_id == created.admin_id
    assert adopted.adopted is True
    assert repeated.admin_id == created.admin_id
    assert repeated.adopted is False
    with database.session_factory() as session:
        administrators = list(session.scalars(select(AdminUserRecord)))
        stored_session = session.scalar(select(AdminSessionRecord))
        audit_events = list(
            session.scalars(select(AuditEventRecord).order_by(AuditEventRecord.id))
        )
        assert len(administrators) == 1
        assert administrators[0].id == int(created.admin_id)
        assert administrators[0].username == "admin"
        assert administrators[0].status == "active"
        assert administrators[0].must_change_password is True
        assert PasswordService().verify(
            administrators[0].password_hash,
            "sk532359025",
        ) is True
        assert PasswordService().verify(
            administrators[0].password_hash,
            "MustNotRotateAgain123",
        ) is False
        assert stored_session is not None and stored_session.revoked_at is not None
        assert len(audit_events) == 2
        assert audit_events[-1].action == "admin_auth.bootstrap.adopt_sole_admin"
        assert audit_events[-1].actor_admin_id == int(created.admin_id)
        assert audit_events[-1].before_summary["username"] == "legacy-admin"
        assert audit_events[-1].after_summary["username"] == "admin"
        assert session.scalar(select(func.count(UserRecord.id))) == 0
        assert session.scalar(select(func.count(WorkspaceRecord.id))) == 0
        assert session.scalar(select(func.count(TicketWalletRecord.id))) == 0
    database.engine.dispose()


def test_explicit_credential_recovery_rotates_password_and_revokes_sessions() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)
    created = service.bootstrap("admin", "InitialSecurePass123")
    issued = issue_admin_session(
        int(created.admin_id),
        "s" * 32,
        policy=SessionPolicy(),
    )
    with database.session_factory.begin() as session:
        session.add(issued.record)

    recovered = service.rotate_existing_password("ADMIN", "RecoveredSecurePass123")

    assert recovered.admin_id == created.admin_id
    assert recovered.created is False
    with database.session_factory() as session:
        admin = session.get(AdminUserRecord, int(created.admin_id))
        stored_session = session.scalar(select(AdminSessionRecord))
        assert admin is not None
        assert PasswordService().verify(admin.password_hash, "InitialSecurePass123") is False
        assert PasswordService().verify(admin.password_hash, "RecoveredSecurePass123") is True
        assert admin.status == "active"
        assert admin.must_change_password is True
        assert stored_session is not None and stored_session.revoked_at is not None
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 2
        assert session.scalar(select(func.count(UserRecord.id))) == 0
        assert session.scalar(select(func.count(WorkspaceRecord.id))) == 0
    database.engine.dispose()


def test_explicit_credential_recovery_requires_existing_admin() -> None:
    database = _database()

    with pytest.raises(BootstrapAdminNotFoundError, match="不存在"):
        PlatformAdminBootstrapService(database).rotate_existing_password(
            "admin",
            "RecoveredSecurePass123",
        )

    database.engine.dispose()


def test_explicit_adoption_preserves_sole_admin_identity_and_revokes_sessions() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database)
    created = service.bootstrap("legacy-admin", "InitialSecurePass123")
    issued = issue_admin_session(
        int(created.admin_id),
        "a" * 32,
        policy=SessionPolicy(),
    )
    with database.session_factory.begin() as session:
        session.add(issued.record)

    adopted = service.adopt_existing_sole_admin(
        "ADMIN",
        "sk532359025",
        allow_local_default_password=True,
    )

    assert adopted.admin_id == created.admin_id
    assert adopted.username == "admin"
    assert adopted.created is False
    with database.session_factory() as session:
        administrators = list(session.scalars(select(AdminUserRecord)))
        stored_session = session.scalar(select(AdminSessionRecord))
        audit_events = list(
            session.scalars(select(AuditEventRecord).order_by(AuditEventRecord.id))
        )
        assert len(administrators) == 1
        assert administrators[0].id == int(created.admin_id)
        assert administrators[0].username == "admin"
        assert administrators[0].status == "active"
        assert administrators[0].must_change_password is True
        assert PasswordService().verify(
            administrators[0].password_hash,
            "sk532359025",
        ) is True
        assert stored_session is not None and stored_session.revoked_at is not None
        assert len(audit_events) == 2
        assert audit_events[-1].action == "admin_auth.credential.adopt_sole_admin"
        assert audit_events[-1].actor_admin_id == int(created.admin_id)
        assert audit_events[-1].before_summary["username"] == "legacy-admin"
        assert audit_events[-1].after_summary["username"] == "admin"
        assert session.scalar(select(func.count(UserRecord.id))) == 0
        assert session.scalar(select(func.count(WorkspaceRecord.id))) == 0
        assert session.scalar(select(func.count(TicketWalletRecord.id))) == 0
    database.engine.dispose()


def test_explicit_adoption_requires_exactly_one_existing_admin() -> None:
    empty_database = _database()
    with pytest.raises(BootstrapSoleAdminRecoveryError, match="恰好存在一个"):
        PlatformAdminBootstrapService(empty_database).adopt_existing_sole_admin(
            "admin",
            "RecoveredSecurePass123",
        )
    empty_database.engine.dispose()

    database = _database()
    service = PlatformAdminBootstrapService(database)
    service.bootstrap("first-admin", "InitialSecurePass123")
    with database.session_factory.begin() as session:
        session.add(
            AdminUserRecord(
                username="second-admin",
                password_hash=PasswordService().hash("SecondSecurePass123"),
                status="active",
            )
        )

    with pytest.raises(BootstrapSoleAdminRecoveryError, match="恰好存在一个"):
        service.adopt_existing_sole_admin("admin", "RecoveredSecurePass123")

    with database.session_factory() as session:
        assert session.scalar(select(func.count(AdminUserRecord.id))) == 2
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 1
    database.engine.dispose()
