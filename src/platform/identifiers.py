from __future__ import annotations

from typing import Any


MAX_DATABASE_ID = 9_223_372_036_854_775_807


def parse_database_id(
    value: Any,
    *,
    field: str = "ID",
    allow_zero: bool = False,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field} 必须是十进制整数")
    minimum = 0 if allow_zero else 1
    if parsed < minimum or parsed > MAX_DATABASE_ID:
        raise ValueError(f"{field} 超出有效范围")
    return parsed


def parse_optional_database_id(value: Any, *, field: str = "ID") -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_database_id(value, field=field)
