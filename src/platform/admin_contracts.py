from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ADMIN_DEFAULT_PAGE_SIZE = 30
ADMIN_MAX_PAGE_SIZE = 100
ADMIN_EXPORT_ROW_CAP = 10_000
ADMIN_MAX_DASHBOARD_DAYS = 93
ADMIN_MAX_SEARCH_LENGTH = 120

ManualRechargeOrderStatus = Literal[
    "pending",
    "completed",
    "cancelled",
    "partially_refunded",
    "refunded",
]
ManualRechargeOrderEventType = Literal["created", "completed", "cancelled", "refunded"]
SystemSceneVisibility = Literal["enabled", "disabled"]

ORDER_STATUS_ZH: dict[str, str] = {
    "pending": "待确认",
    "completed": "已完成",
    "cancelled": "已取消",
    "partially_refunded": "部分退款",
    "refunded": "已退款",
}

ORDER_EVENT_ZH: dict[str, str] = {
    "created": "创建订单",
    "completed": "人工确认到账",
    "cancelled": "取消订单",
    "refunded": "登记退款",
}

ADMIN_NAVIGATION_GROUPS = (
    ("dashboard", "运营概览"),
    ("users", "用户管理"),
    ("finance", "财务管理"),
    ("content", "内容与资产"),
    ("ai", "AI 运营"),
    ("platform", "平台配置"),
    ("operations", "审计与运维"),
)

_CJK = re.compile(r"[\u3400-\u9fff]")


def normalize_chinese_reason(value: str, *, field_name: str = "操作原因") -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name}不能为空")
    if not _CJK.search(normalized):
        raise ValueError(f"{field_name}必须包含中文说明")
    return normalized


def validate_bounded_window(
    start_at: datetime,
    end_at: datetime,
    *,
    max_days: int = ADMIN_MAX_DASHBOARD_DAYS,
) -> tuple[datetime, datetime]:
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=UTC)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)
    if end_at <= start_at:
        raise ValueError("结束时间必须晚于开始时间")
    if end_at - start_at > timedelta(days=max_days):
        raise ValueError(f"查询时间范围不能超过 {max_days} 天")
    return start_at, end_at


class AdminReasonMixin(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return normalize_chinese_reason(value)


class SystemScenePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    category: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    style: str = Field(default="通用", min_length=1, max_length=80)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4"] = "16:9"
    visibility: SystemSceneVisibility = "disabled"
    sort_order: int = Field(default=100, ge=0, le=1_000_000)
    schema_version: int = Field(default=1, ge=1, le=100)
    cover_media_id: int | None = Field(default=None, ge=1)

    @field_validator("name", "description")
    @classmethod
    def require_chinese_display_text(cls, value: str) -> str:
        normalized = value.strip()
        if not _CJK.search(normalized):
            raise ValueError("名称和描述必须包含中文")
        return normalized

    @field_validator("category", "style", "prompt", "negative_prompt")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            tag = value.strip()
            if not tag or len(tag) > 40:
                raise ValueError("标签不能为空且不能超过 40 个字符")
            if tag not in normalized:
                normalized.append(tag)
        return normalized
