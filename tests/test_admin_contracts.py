from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from src.platform.admin_contracts import (
    ADMIN_EXPORT_ROW_CAP,
    ADMIN_MAX_DASHBOARD_DAYS,
    ADMIN_MAX_PAGE_SIZE,
    ORDER_STATUS_ZH,
    SystemScenePayload,
    normalize_chinese_reason,
    validate_bounded_window,
)


def test_admin_limits_and_chinese_order_labels_are_stable() -> None:
    assert ADMIN_MAX_PAGE_SIZE == 100
    assert ADMIN_EXPORT_ROW_CAP == 10_000
    assert ORDER_STATUS_ZH["partially_refunded"] == "部分退款"


def test_admin_reason_must_contain_chinese_explanation() -> None:
    assert normalize_chinese_reason("  客服确认线下到账  ") == "客服确认线下到账"
    with pytest.raises(ValueError, match="中文"):
        normalize_chinese_reason("manual recharge")


def test_dashboard_window_is_bounded() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert validate_bounded_window(start, start + timedelta(days=1))[0] == start
    with pytest.raises(ValueError, match=str(ADMIN_MAX_DASHBOARD_DAYS)):
        validate_bounded_window(start, start + timedelta(days=ADMIN_MAX_DASHBOARD_DAYS + 1))


def test_system_scene_payload_rejects_unknown_or_non_chinese_display_fields() -> None:
    valid = SystemScenePayload(
        name="雨夜街角",
        description="适合都市悬疑故事的夜景",
        category="都市",
        tags=["夜景", "雨天", "夜景"],
        prompt="cinematic rainy street",
    )
    assert valid.tags == ["夜景", "雨天"]
    with pytest.raises(ValidationError):
        SystemScenePayload(
            name="Rain street",
            description="English only",
            category="city",
            prompt="rain",
            unsupported=True,
        )


def test_first_admin_release_excludes_forbidden_platform_capabilities() -> None:
    inspection_api = Path("src/platform/admin_inspection_api.py").read_text(
        encoding="utf-8"
    )
    system_scene_api = Path("src/platform/system_scenes_api.py").read_text(
        encoding="utf-8"
    )
    system_scene_service = Path("src/platform/system_scenes.py").read_text(
        encoding="utf-8"
    )
    recharge_api = Path("src/platform/manual_recharge_api.py").read_text(
        encoding="utf-8"
    )
    database_models = Path("src/platform/db_models.py").read_text(encoding="utf-8")

    inspection_routes = re.findall(
        r'@(?:app|router)\.(get|post|put|patch|delete)\("([^"]+)"',
        inspection_api,
    )
    resource_routes = [
        (method, route)
        for method, route in inspection_routes
        if "/resources/" in route
    ]
    assert resource_routes
    assert all(method == "get" for method, _route in resource_routes)

    combined_routes = "\n".join((inspection_api, system_scene_api, recharge_api)).lower()
    assert "impersonat" not in combined_routes
    assert "login-as" not in combined_routes
    assert "payment_provider" not in combined_routes
    assert "webhook" not in combined_routes
    assert "@app.delete" not in system_scene_api
    assert "session.delete" not in system_scene_service
    assert "admin_roles" not in database_models
    assert "admin_permissions" not in database_models
