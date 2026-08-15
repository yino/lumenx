from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from src.platform.configuration_schemas import ConfigurationDraft, RegistrationMode
from src.platform.configuration_service import (
    ConfigurationConflictError,
    ConfigurationService,
    ConfigurationValidationError,
)
from src.platform.contracts import UserContext
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from tests.test_configuration_schemas import _image_route, _platform
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def configuration_service():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    admin = replace(context.identity, is_platform_admin=True)
    yield database, admin, ConfigurationService(database)
    database.engine.dispose()


def _draft(
    *,
    reason: str = "配置平台图像能力",
    route_updates: dict | None = None,
) -> ConfigurationDraft:
    route = _image_route(**(route_updates or {}))
    return ConfigurationDraft.model_validate(
        {
            "reason": reason,
            "platform": _platform(),
            "routes": [route],
        }
    )


def test_configuration_versions_are_created_with_typed_rows_and_safe_audit(
    configuration_service,
) -> None:
    database, admin, service = configuration_service

    stored = service.create_version(admin, _draft())

    assert stored.version_number == 1
    assert stored.status == "draft"
    assert stored.draft.routes[0].provider_model_id == "wan-image-v1"
    with database.session_factory() as session:
        assert session.scalar(select(ConfigVersionRecord)) is not None
        assert session.scalar(select(PlatformConfigRecord)) is not None
        assert session.scalar(select(ModelConfigRecord)) is not None
        audit = session.scalar(select(AuditEventRecord))
        assert audit is not None
        assert audit.action == "configuration.create"
        assert "secret" not in str(audit.after_summary).lower()
        assert "DASHSCOPE_API_KEY" not in str(audit.after_summary)


def test_activation_validates_complete_primary_routes_atomically(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    incomplete = ConfigurationDraft.model_validate(
        {
            "reason": "缺少主路由",
            "platform": _platform(),
            "routes": [_image_route(enabled=False, is_primary=False)],
        }
    )
    stored = service.create_version(admin, incomplete)

    with pytest.raises(ConfigurationValidationError, match="一个启用的主路由"):
        service.activate_version(admin, stored.id, reason="尝试激活")

    assert service.get_version(admin, stored.id).status == "draft"


def test_fail_closed_configuration_can_activate_without_model_routes(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    draft = ConfigurationDraft.model_validate(
        {
            "reason": "初始化安全运行策略",
            "platform": {
                **_platform(),
                "exposed_capabilities": [],
                "feature_flags": {
                    "registration_mode": "disabled",
                    "new_ai_tasks_enabled": False,
                },
            },
            "routes": [],
        }
    )

    stored = service.create_version(admin, draft)
    activated = service.activate_version(admin, stored.id, reason="启用安全策略")

    assert activated.status == "active"
    assert activated.draft.platform.exposed_capabilities == []
    assert activated.draft.routes == []


def test_activation_rejects_verified_open_without_verification_provider(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    draft = _draft(reason="错误开放自助注册")
    draft = draft.model_copy(
        update={
            "platform": draft.platform.model_copy(
                update={
                    "feature_flags": draft.platform.feature_flags.model_copy(
                        update={"registration_mode": RegistrationMode.VERIFIED_OPEN}
                    )
                }
            )
        }
    )
    stored = service.create_version(admin, draft)

    with pytest.raises(ConfigurationValidationError, match="验证码服务尚未启用"):
        service.activate_version(admin, stored.id, reason="尝试开放注册")


def test_activation_allows_open_registration_without_verification_provider(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    draft = _draft(reason="开放手机号密码注册")
    draft = draft.model_copy(
        update={
            "platform": draft.platform.model_copy(
                update={
                    "feature_flags": draft.platform.feature_flags.model_copy(
                        update={"registration_mode": RegistrationMode.OPEN}
                    )
                }
            )
        }
    )

    stored = service.create_version(admin, draft)
    activated = service.activate_version(admin, stored.id, reason="开放自助注册")

    assert activated.draft.platform.feature_flags.registration_mode is RegistrationMode.OPEN


def test_failed_activation_keeps_current_version_active(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    current = service.create_version(admin, _draft(reason="当前有效配置"))
    service.activate_version(admin, current.id, reason="启用当前配置")
    invalid = ConfigurationDraft.model_validate(
        {
            "reason": "包含两个主路由",
            "platform": _platform(),
            "routes": [
                _image_route(provider_model_id="wan-primary-a", priority=10),
                _image_route(provider_model_id="wan-primary-b", priority=20),
            ],
        }
    )
    candidate = service.create_version(admin, invalid)

    with pytest.raises(ConfigurationValidationError, match="一个启用的主路由"):
        service.activate_version(admin, candidate.id, reason="尝试启用无效配置")

    assert service.get_active(admin).id == current.id
    assert service.get_version(admin, current.id).status == "active"
    assert service.get_version(admin, candidate.id).status == "draft"


def test_activation_rejects_formula_that_does_not_match_capability(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    mismatch = _draft(
        reason="公式类型不匹配",
        route_updates={
            "metering_formula": {
                "kind": "llm",
                "input_weight": 1,
                "output_weight": 2,
                "max_input_tokens": 4096,
                "max_output_tokens": 2048,
            }
        },
    )
    stored = service.create_version(admin, mismatch)

    with pytest.raises(ConfigurationValidationError, match="计量公式与能力不匹配"):
        service.activate_version(admin, stored.id, reason="尝试启用错误公式")


def test_activation_supersedes_previous_version_and_invalidates_cache(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    first = service.create_version(admin, _draft(reason="第一版"))
    activated_first = service.activate_version(admin, first.id, reason="启用第一版")
    assert service.get_active(admin).id == activated_first.id

    second = service.create_version(
        admin,
        _draft(
            reason="第二版",
            route_updates={"provider_model_id": "wan-image-v2"},
        ),
    )
    activated_second = service.activate_version(admin, second.id, reason="启用第二版")

    assert activated_second.version_number == 2
    assert service.get_active(admin).id == activated_second.id
    assert service.get_active(admin).draft.routes[0].provider_model_id == "wan-image-v2"
    assert service.get_version(admin, first.id).status == "superseded"
    with pytest.raises(ConfigurationConflictError, match="仅草稿"):
        service.activate_version(admin, first.id, reason="错误重复激活")


def test_separate_runtime_reader_observes_new_active_version(
    configuration_service,
) -> None:
    database, admin, writer = configuration_service
    reader = ConfigurationService(database)
    first = writer.create_version(admin, _draft(reason="跨实例第一版"))
    writer.activate_version(admin, first.id, reason="启用跨实例第一版")
    assert reader.get_active_for_runtime(admin).id == first.id

    second = writer.create_version(
        admin,
        _draft(
            reason="跨实例第二版",
            route_updates={"provider_model_id": "wan-image-cross-instance-v2"},
        ),
    )
    writer.activate_version(admin, second.id, reason="启用跨实例第二版")

    observed = reader.get_active_for_runtime(admin)
    assert observed.id == second.id
    assert observed.draft.routes[0].provider_model_id == "wan-image-cross-instance-v2"


def test_draft_can_be_validated_and_disabled_with_audit(
    configuration_service,
) -> None:
    database, admin, service = configuration_service
    stored = service.create_version(admin, _draft(reason="准备停用的草稿"))

    validated = service.validate_version(admin, stored.id)
    disabled = service.disable_version(admin, stored.id, reason="草稿不再使用")

    assert validated.id == stored.id
    assert disabled.status == "disabled"
    with pytest.raises(ConfigurationConflictError, match="已停用"):
        service.validate_version(admin, stored.id)
    with pytest.raises(ConfigurationConflictError, match="仅草稿"):
        service.activate_version(admin, stored.id, reason="不能激活已停用版本")
    with database.session_factory() as session:
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "configuration.disable"
            )
        )
        assert audit is not None
        assert audit.reason == "草稿不再使用"
        assert audit.before_summary["status"] == "draft"
        assert audit.after_summary["status"] == "disabled"


def test_rollback_creates_and_activates_a_new_version(configuration_service) -> None:
    _database, admin, service = configuration_service
    first = service.create_version(admin, _draft(reason="第一版"))
    service.activate_version(admin, first.id, reason="启用第一版")
    second = service.create_version(
        admin,
        _draft(
            reason="第二版",
            route_updates={"provider_model_id": "wan-image-v2"},
        ),
    )
    service.activate_version(admin, second.id, reason="启用第二版")

    rolled_back = service.rollback_to_new_version(
        admin,
        first.id,
        reason="回滚第一版能力配置",
    )

    assert rolled_back.version_number == 3
    assert rolled_back.id not in {first.id, second.id}
    assert rolled_back.draft.routes[0].provider_model_id == "wan-image-v1"
    assert service.get_version(admin, first.id).status == "superseded"
    assert service.get_version(admin, second.id).status == "superseded"


def test_configuration_administration_requires_platform_admin(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service
    normal_user = UserContext(
        user_id=admin.user_id,
        session_id=admin.session_id,
        is_platform_admin=False,
    )

    with pytest.raises(PermissionError, match="平台管理员"):
        service.create_version(normal_user, _draft())


def test_configuration_audit_metadata_rejects_plaintext_secrets(
    configuration_service,
) -> None:
    _database, admin, service = configuration_service

    with pytest.raises(ValueError, match="明文凭据字段"):
        service.create_version(
            admin,
            _draft(),
            audit_metadata={
                "provider_request": {
                    "authorization": "Bearer provider-secret",
                }
            },
        )

    assert service.list_versions(admin) == []
