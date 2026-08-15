#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from src.platform.configuration_schemas import RegistrationMode
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import UserContext
from src.platform.database import Database
from src.platform.db_models import UserRecord
from src.platform.settings import DeploymentMode, get_deployment_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过版本化平台配置切换用户注册模式"
    )
    parser.add_argument(
        "mode",
        choices=[mode.value for mode in RegistrationMode],
        help="disabled、invite_only、open 或 verified_open",
    )
    parser.add_argument("--reason", required=True, help="写入配置和审计记录的变更原因")
    args = parser.parse_args()

    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("注册模式切换仅支持已配置 PostgreSQL 的云端模式")
    mode = RegistrationMode(args.mode)
    if mode is not RegistrationMode.DISABLED and settings.registration_emergency_disabled:
        raise RuntimeError(
            "注册紧急熔断仍处于开启状态，请先设置 "
            "LUMENX_REGISTRATION_EMERGENCY_DISABLED=false"
        )

    database = Database(settings.database_url)
    system_identity = UserContext(
        user_id="0",
        session_id="registration-mode-operator",
        is_platform_admin=True,
    )
    try:
        with database.transaction(system_identity) as session:
            admin_id = session.scalar(
                select(UserRecord.id)
                .where(
                    UserRecord.is_platform_admin.is_(True),
                    UserRecord.status == "active",
                )
                .order_by(UserRecord.created_at.asc(), UserRecord.id.asc())
                .limit(1)
            )
        if admin_id is None:
            raise RuntimeError("没有可用于发布配置的有效平台管理员")

        admin = UserContext(
            user_id=str(admin_id),
            session_id="registration-mode-operator",
            is_platform_admin=True,
        )
        service = ConfigurationService(database, verification_provider_available=False)
        active = service.get_active(admin)
        previous_mode = active.draft.platform.feature_flags.registration_mode
        if previous_mode is mode:
            print(
                json.dumps(
                    {
                        "changed": False,
                        "config_version_id": active.id,
                        "registration_mode": mode.value,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        feature_flags = active.draft.platform.feature_flags.model_copy(
            update={"registration_mode": mode}
        )
        platform = active.draft.platform.model_copy(
            update={"feature_flags": feature_flags}
        )
        draft = active.draft.model_copy(
            update={"reason": args.reason.strip(), "platform": platform}
        )
        created = service.create_version(admin, draft)
        activated = service.activate_version(
            admin,
            created.id,
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "changed": True,
                    "config_version_id": activated.id,
                    "version_number": activated.version_number,
                    "previous_registration_mode": previous_mode.value,
                    "registration_mode": mode.value,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
