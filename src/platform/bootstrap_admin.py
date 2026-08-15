from __future__ import annotations

import argparse
import getpass
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text

from .auth.registration import RegistrationPolicy, RegistrationService
from .auth.security import normalize_phone
from .configuration_schemas import RegistrationMode
from .contracts import UserContext
from .database import Database
from .db_models import AuditEventRecord, UserRecord
from .settings import DeploymentMode, get_deployment_settings


@dataclass(frozen=True, slots=True)
class BootstrapAdminResult:
    user_id: str
    phone_canonical: str
    created: bool
    promoted: bool


class BootstrapPasswordRequiredError(ValueError):
    pass


class BootstrapAlreadyCompletedError(RuntimeError):
    pass


class PlatformAdminBootstrapService:
    def __init__(
        self,
        database: Database,
        session_secret: str,
        *,
        initial_grant_microtickets: int = 0,
    ) -> None:
        self.database = database
        self.registration = RegistrationService(
            database,
            session_secret,
            policy=RegistrationPolicy(
                initial_grant_microtickets=initial_grant_microtickets,
                registration_mode=RegistrationMode.OPEN,
            ),
        )

    def bootstrap(self, phone: str, password: str | None = None) -> BootstrapAdminResult:
        phone_canonical = normalize_phone(phone)
        bootstrap_identity = UserContext(
            user_id="0",
            session_id="platform-admin-bootstrap",
            is_platform_admin=True,
        )
        with self.database.transaction(bootstrap_identity) as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(1280785237)"))
            existing_admin = session.scalar(
                select(UserRecord)
                .where(UserRecord.is_platform_admin.is_(True))
                .order_by(UserRecord.created_at.asc(), UserRecord.id.asc())
                .limit(1)
                .with_for_update()
            )
            existing = session.scalar(
                select(UserRecord).where(UserRecord.phone_canonical == phone_canonical)
            )
            if existing_admin is not None and (
                existing is None or existing.id != existing_admin.id
            ):
                raise BootstrapAlreadyCompletedError(
                    "首个平台管理员已完成初始化，不能通过初始化命令提升其他账号"
                )
        created = existing is None
        if existing is None:
            if password is None:
                raise BootstrapPasswordRequiredError(
                    "创建首个平台管理员时必须提供初始密码"
                )
            registered = self.registration.register(phone_canonical, password)
            user_id = registered.user_id
        else:
            user_id = existing.id

        identity = UserContext(
            user_id=str(user_id),
            session_id="platform-admin-bootstrap",
            is_platform_admin=True,
        )
        promoted = False
        with self.database.transaction(identity) as session:
            user = session.get(UserRecord, user_id)
            if user is None:
                raise RuntimeError("首个平台管理员用户不存在")
            if user.status != "active":
                raise RuntimeError("不能把已停用用户设为平台管理员")
            if not user.is_platform_admin:
                user.is_platform_admin = True
                promoted = True
            session.add(
                AuditEventRecord(
                    actor_user_id=user.id,
                    target_user_id=user.id,
                    action="auth.bootstrap_admin",
                    target_type="user",
                    target_id=str(user.id),
                    reason="首个平台管理员初始化",
                    before_summary={"is_platform_admin": not promoted},
                    after_summary={
                        "is_platform_admin": True,
                        "created": created,
                        "operational_exception": "first_admin_bootstrap",
                    },
                    correlation_id=str(uuid.uuid4()),
                )
            )
        return BootstrapAdminResult(
            user_id=str(user_id),
            phone_canonical=phone_canonical,
            created=created,
            promoted=promoted,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="幂等创建或提升 LumenX 平台管理员")
    parser.add_argument("--phone", required=True)
    parser.add_argument(
        "--password",
        help="仅新建用户时使用；省略后从终端安全读取，不写入命令历史",
    )
    args = parser.parse_args()
    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("平台管理员初始化仅支持已配置 PostgreSQL 的云端模式")
    database = Database(settings.database_url)
    try:
        service = PlatformAdminBootstrapService(
            database,
            settings.session_secret.get_secret_value(),
            initial_grant_microtickets=settings.registration_initial_grant_microtickets,
        )
        try:
            result = service.bootstrap(args.phone, args.password)
        except BootstrapPasswordRequiredError:
            password = getpass.getpass("用户不存在，请输入初始密码：")
            result = service.bootstrap(args.phone, password)
        state = "已创建并设为管理员" if result.created else (
            "已提升为管理员" if result.promoted else "已经是管理员"
        )
        print(f"{state}：{result.phone_canonical}，用户 ID {result.user_id}")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
