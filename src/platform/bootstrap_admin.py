from __future__ import annotations

import argparse
import getpass
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .auth.security import PasswordService, normalize_username
from .contracts import SystemContext
from .database import Database
from .db_models import AdminSessionRecord, AdminUserRecord, AuditEventRecord
from .settings import DeploymentMode, get_deployment_settings


LOCAL_DEFAULT_ADMIN_PASSWORD = "sk532359025"


@dataclass(frozen=True, slots=True)
class BootstrapAdminResult:
    admin_id: str
    username: str
    created: bool
    adopted: bool = False


class BootstrapPasswordRequiredError(ValueError):
    pass


class BootstrapAlreadyCompletedError(RuntimeError):
    pass


class InsecureBootstrapPasswordError(ValueError):
    pass


class BootstrapAdminNotFoundError(LookupError):
    pass


class BootstrapSoleAdminRecoveryError(RuntimeError):
    pass


class PlatformAdminBootstrapService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.password_service = PasswordService()

    def bootstrap(
        self,
        username: str,
        password: str | None,
        *,
        allow_local_default_password: bool = False,
        adopt_existing_sole_admin: bool = False,
    ) -> BootstrapAdminResult:
        canonical_username = normalize_username(username)
        if password is None:
            raise BootstrapPasswordRequiredError("创建系统管理员时必须提供初始密码")
        if (
            password == LOCAL_DEFAULT_ADMIN_PASSWORD
            and not allow_local_default_password
        ):
            raise InsecureBootstrapPasswordError(
                "非本地环境禁止使用默认管理员密码"
            )

        identity = SystemContext(service_name="admin-bootstrap")
        with self.database.transaction(identity) as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(1280785237)"))
            administrators = list(
                session.scalars(
                    select(AdminUserRecord)
                    .order_by(AdminUserRecord.id)
                    .with_for_update()
                )
            )
            existing = next(
                (
                    admin
                    for admin in administrators
                    if admin.username == canonical_username
                ),
                None,
            )
            if existing is not None:
                return BootstrapAdminResult(
                    admin_id=str(existing.id),
                    username=existing.username,
                    created=False,
                )
            if administrators:
                if adopt_existing_sole_admin and len(administrators) == 1:
                    return self._adopt_locked_admin(
                        session,
                        administrators[0],
                        canonical_username=canonical_username,
                        password=password,
                        action="admin_auth.bootstrap.adopt_sole_admin",
                        reason="本地 Compose 自动接管唯一系统管理员身份",
                    )
                raise BootstrapAlreadyCompletedError(
                    "系统管理员已完成初始化，不能通过初始化命令创建第二个管理员"
                )
            admin = AdminUserRecord(
                username=canonical_username,
                password_hash=self.password_service.hash(password),
                status="active",
                must_change_password=True,
            )
            session.add(admin)
            session.flush()
            session.add(
                AuditEventRecord(
                    actor_admin_id=admin.id,
                    action="admin_auth.bootstrap",
                    target_type="admin_user",
                    target_id=str(admin.id),
                    reason="系统管理员初始化",
                    after_summary={
                        "username": admin.username,
                        "created": True,
                        "must_change_password": True,
                    },
                    correlation_id=str(uuid.uuid4()),
                )
            )
            return BootstrapAdminResult(
                admin_id=str(admin.id),
                username=admin.username,
                created=True,
            )

    def _adopt_locked_admin(
        self,
        session: Session,
        admin: AdminUserRecord,
        *,
        canonical_username: str,
        password: str,
        action: str,
        reason: str,
    ) -> BootstrapAdminResult:
        recovered_at = datetime.now(UTC)
        previous_username = admin.username
        previous_status = admin.status
        admin.username = canonical_username
        admin.password_hash = self.password_service.hash(password)
        admin.password_changed_at = recovered_at
        admin.status = "active"
        admin.must_change_password = True
        session.execute(
            update(AdminSessionRecord)
            .where(
                AdminSessionRecord.admin_user_id == admin.id,
                AdminSessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=recovered_at)
        )
        session.add(
            AuditEventRecord(
                actor_admin_id=admin.id,
                action=action,
                target_type="admin_user",
                target_id=str(admin.id),
                reason=reason,
                before_summary={
                    "username": previous_username,
                    "status": previous_status,
                },
                after_summary={
                    "username": admin.username,
                    "status": admin.status,
                    "sessions_revoked": True,
                    "must_change_password": True,
                },
                correlation_id=str(uuid.uuid4()),
            )
        )
        return BootstrapAdminResult(
            admin_id=str(admin.id),
            username=admin.username,
            created=False,
            adopted=True,
        )

    def rotate_existing_password(
        self,
        username: str,
        password: str | None,
        *,
        allow_local_default_password: bool = False,
    ) -> BootstrapAdminResult:
        canonical_username = normalize_username(username)
        if password is None:
            raise BootstrapPasswordRequiredError("恢复系统管理员时必须提供新密码")
        if (
            password == LOCAL_DEFAULT_ADMIN_PASSWORD
            and not allow_local_default_password
        ):
            raise InsecureBootstrapPasswordError("非本地环境禁止使用默认管理员密码")

        recovered_at = datetime.now(UTC)
        identity = SystemContext(service_name="admin-credential-recovery")
        with self.database.transaction(identity) as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(1280785237)"))
            admin = session.scalar(
                select(AdminUserRecord)
                .where(AdminUserRecord.username == canonical_username)
                .limit(1)
                .with_for_update()
            )
            if admin is None:
                raise BootstrapAdminNotFoundError("指定的系统管理员不存在，无法恢复凭据")
            admin.password_hash = self.password_service.hash(password)
            admin.password_changed_at = recovered_at
            admin.status = "active"
            admin.must_change_password = True
            session.execute(
                update(AdminSessionRecord)
                .where(
                    AdminSessionRecord.admin_user_id == admin.id,
                    AdminSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=recovered_at)
            )
            session.add(
                AuditEventRecord(
                    actor_admin_id=admin.id,
                    action="admin_auth.credential.recover",
                    target_type="admin_user",
                    target_id=str(admin.id),
                    reason="显式管理员凭据恢复",
                    after_summary={
                        "username": admin.username,
                        "sessions_revoked": True,
                        "must_change_password": True,
                    },
                    correlation_id=str(uuid.uuid4()),
                )
            )
            return BootstrapAdminResult(
                admin_id=str(admin.id),
                username=admin.username,
                created=False,
            )

    def adopt_existing_sole_admin(
        self,
        username: str,
        password: str | None,
        *,
        allow_local_default_password: bool = False,
    ) -> BootstrapAdminResult:
        canonical_username = normalize_username(username)
        if password is None:
            raise BootstrapPasswordRequiredError("接管系统管理员时必须提供新密码")
        if (
            password == LOCAL_DEFAULT_ADMIN_PASSWORD
            and not allow_local_default_password
        ):
            raise InsecureBootstrapPasswordError("非本地环境禁止使用默认管理员密码")

        identity = SystemContext(service_name="admin-sole-identity-adoption")
        with self.database.transaction(identity) as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(1280785237)"))
            administrators = list(
                session.scalars(
                    select(AdminUserRecord)
                    .order_by(AdminUserRecord.id)
                    .with_for_update()
                )
            )
            if len(administrators) != 1:
                raise BootstrapSoleAdminRecoveryError(
                    "显式接管要求数据库中恰好存在一个系统管理员"
                )

            return self._adopt_locked_admin(
                session,
                administrators[0],
                canonical_username=canonical_username,
                password=password,
                action="admin_auth.credential.adopt_sole_admin",
                reason="显式接管唯一系统管理员身份",
            )


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="幂等创建 LumenX 独立系统管理员")
    parser.add_argument(
        "--username",
        default=os.getenv("LUMENX_BOOTSTRAP_ADMIN_USERNAME", "admin"),
    )
    parser.add_argument(
        "--password",
        help="仅首次创建时使用；省略后从环境变量或终端读取",
    )
    parser.add_argument(
        "--password-env",
        default="LUMENX_BOOTSTRAP_ADMIN_PASSWORD",
        help="初始密码环境变量名",
    )
    recovery_group = parser.add_mutually_exclusive_group()
    recovery_group.add_argument(
        "--rotate-existing",
        action="store_true",
        help="显式恢复现有管理员密码并撤销其全部后台会话",
    )
    recovery_group.add_argument(
        "--adopt-existing-sole-admin",
        action="store_true",
        help="显式把唯一现有管理员接管为指定用户名并恢复凭据",
    )
    args = parser.parse_args()
    settings = get_deployment_settings()
    if settings.deployment_mode not in {DeploymentMode.CLOUD, DeploymentMode.TEST}:
        raise RuntimeError("系统管理员初始化仅支持 PostgreSQL 云端或发布测试模式")
    if not settings.database_url:
        raise RuntimeError("系统管理员初始化缺少 PostgreSQL 连接")

    password = args.password or os.getenv(args.password_env)
    if password is None:
        password = getpass.getpass("请输入系统管理员初始密码：")
    allow_default = _env_true("LUMENX_ALLOW_DEFAULT_BOOTSTRAP_ADMIN_PASSWORD")
    adopt_on_local_bootstrap = _env_true(
        "LUMENX_BOOTSTRAP_ADMIN_ADOPT_EXISTING_SOLE"
    )
    if adopt_on_local_bootstrap and not allow_default:
        raise RuntimeError("非本地环境禁止自动接管现有系统管理员")
    database = Database(settings.database_url)
    try:
        service = PlatformAdminBootstrapService(database)
        if args.adopt_existing_sole_admin:
            result = service.adopt_existing_sole_admin(
                args.username,
                password,
                allow_local_default_password=allow_default,
            )
            state = "唯一身份已接管"
        elif args.rotate_existing:
            result = service.rotate_existing_password(
                args.username,
                password,
                allow_local_default_password=allow_default,
            )
            state = "凭据已恢复"
        else:
            result = service.bootstrap(
                args.username,
                password,
                allow_local_default_password=allow_default,
                adopt_existing_sole_admin=adopt_on_local_bootstrap,
            )
            if result.adopted:
                state = "唯一身份已自动接管"
            else:
                state = "已创建" if result.created else "已存在"
        print(f"系统管理员{state}：{result.username}，管理员 ID {result.admin_id}")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
