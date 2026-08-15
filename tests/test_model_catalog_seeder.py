from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.platform.configuration_schemas import ConfigurationDraft, PlatformConfig
from src.platform.configuration_service import (
    ConfigurationService,
    ConfigurationValidationError,
)
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
    UserRecord,
)
from src.platform.model_catalog_seeder import ModelCatalogSeeder
from tests.test_configuration_schemas import _image_route, _platform
from tests.test_content_repositories import RepositoryDatabase, _create_scope


CATALOG_PATH = Path("config/model_catalog/generated/model_catalog.json")


@pytest.fixture
def catalog_seeder():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    admin = replace(context.identity, is_platform_admin=True)
    with database.session_factory.begin() as session:
        user = session.get(UserRecord, int(admin.user_id))
        assert user is not None
        user.is_platform_admin = True
    yield database, admin
    database.engine.dispose()


def _seed_platform() -> PlatformConfig:
    return PlatformConfig.model_validate(_platform())


def test_generated_catalog_maps_to_inactive_reviewable_routes(catalog_seeder) -> None:
    database, admin = catalog_seeder

    stored, created = ModelCatalogSeeder(database, CATALOG_PATH).seed(
        admin,
        _seed_platform(),
        reason="导入仓库模型目录",
    )

    assert created is True
    assert stored.status == "draft"
    assert len(stored.draft.routes) == 45
    assert len(
        {
            (route.capability, route.provider, route.provider_model_id)
            for route in stored.draft.routes
        }
    ) == 45
    assert all(not route.enabled and not route.is_primary for route in stored.draft.routes)
    assert all(route.metering_formula.review_required for route in stored.draft.routes)
    assert {route.secret_ref for route in stored.draft.routes} == {
        "ARK_API_KEY",
        "DASHSCOPE_API_KEY",
        "MULEROUTER_API_KEY",
    }
    assert {item.value for item in stored.draft.platform.exposed_capabilities} == {
        "image.t2i",
        "image.i2i",
        "video.t2v",
        "video.i2v",
        "video.r2v",
        "video.v2v",
    }

    fixed_duration = next(
        route
        for route in stored.draft.routes
        if route.provider_model_id == "wan2.2-i2v-flash"
    )
    duration_rule = next(
        rule for rule in fixed_duration.parameter_schema if rule.name == "duration"
    )
    assert fixed_duration.default_parameters["duration"] == 5
    assert duration_rule.choices == [5]
    assert fixed_duration.metering_formula.max_duration_seconds == 5

    kling = next(
        route
        for route in stored.draft.routes
        if route.provider_model_id == "kling-v3-i2v"
    )
    assert kling.normalize_parameters({"sound": True})["sound"] is True

    with pytest.raises(ConfigurationValidationError, match="启用的主路由"):
        ConfigurationService(database).activate_version(
            admin,
            stored.id,
            reason="未审核前不得激活",
        )


def test_seed_is_idempotent_and_does_not_overwrite_admin_versions(
    catalog_seeder,
    tmp_path: Path,
) -> None:
    database, admin = catalog_seeder
    service = ConfigurationService(database)
    administrator_version = service.create_version(
        admin,
        ConfigurationDraft.model_validate(
            {
                "reason": "管理员现有配置",
                "platform": _platform(),
                "routes": [_image_route()],
            }
        ),
    )
    seeder = ModelCatalogSeeder(database, CATALOG_PATH)

    first, first_created = seeder.seed(
        admin,
        _seed_platform(),
        reason="首次导入",
    )
    repeated, repeated_created = seeder.seed(
        admin,
        _seed_platform(),
        reason="重复导入",
    )

    assert first_created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert service.get_version(admin, administrator_version.id).draft.reason == (
        "管理员现有配置"
    )

    changed_catalog = json.loads(CATALOG_PATH.read_text())
    changed_catalog["version"] = f"{changed_catalog.get('version', 'catalog')}-changed"
    changed_path = tmp_path / "model_catalog.json"
    changed_path.write_text(json.dumps(changed_catalog, ensure_ascii=False))
    changed, changed_created = ModelCatalogSeeder(database, changed_path).seed(
        admin,
        _seed_platform(),
        reason="目录更新后导入",
    )

    assert changed_created is True
    assert changed.id not in {administrator_version.id, first.id}
    assert changed.status == "draft"
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ConfigVersionRecord)) == 3
        seed_events = list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.action == "configuration.seed"
                )
            )
        )
    assert len(seed_events) == 2
    assert all("secret" not in str(event.after_summary).lower() for event in seed_events)
