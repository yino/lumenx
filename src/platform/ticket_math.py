from __future__ import annotations

from dataclasses import dataclass


MICROTICKETS_PER_TICKET = 1_000_000
MAX_BIGINT = (1 << 63) - 1


class TicketArithmeticError(ValueError):
    pass


def _require_integer(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TicketArithmeticError(f"{name}必须是整数")
    return value


def validate_microtickets(value: int, *, name: str = "微算力券金额") -> int:
    amount = _require_integer(value, name)
    if amount < 0:
        raise TicketArithmeticError(f"{name}不能为负数")
    if amount > MAX_BIGINT:
        raise TicketArithmeticError(f"{name}超出可存储范围")
    return amount


def add_microtickets(balance: int, amount: int) -> int:
    current = validate_microtickets(balance, name="当前微算力券余额")
    delta = validate_microtickets(amount, name="增加微算力券金额")
    result = current + delta
    if result > MAX_BIGINT:
        raise TicketArithmeticError("微算力券余额相加后超出可存储范围")
    return result


def subtract_microtickets(balance: int, amount: int) -> int:
    current = validate_microtickets(balance, name="当前微算力券余额")
    delta = validate_microtickets(amount, name="扣减微算力券金额")
    if delta > current:
        raise TicketArithmeticError("微算力券余额不足")
    return current - delta


def metering_tokens_to_microtickets(
    metering_tokens: int,
    tokens_per_ticket: int,
) -> int:
    tokens = _require_integer(metering_tokens, "计量 token")
    exchange_rate = _require_integer(tokens_per_ticket, "每张算力券 token 数")
    if tokens < 0:
        raise TicketArithmeticError("计量 token 不能为负数")
    if tokens > MAX_BIGINT:
        raise TicketArithmeticError("计量 token 超出可存储范围")
    if exchange_rate <= 0:
        raise TicketArithmeticError("每张算力券 token 数必须为正整数")
    if exchange_rate > MAX_BIGINT:
        raise TicketArithmeticError("每张算力券 token 数超出可存储范围")
    if tokens == 0:
        return 0

    numerator = tokens * MICROTICKETS_PER_TICKET
    quotient, remainder = divmod(numerator, exchange_rate)
    result = quotient + (1 if remainder else 0)
    return validate_microtickets(result, name="换算后的微算力券金额")


@dataclass(frozen=True, slots=True)
class TicketConversionSnapshot:
    config_version_id: str
    tokens_per_ticket: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.config_version_id, str) or not self.config_version_id.strip():
            raise TicketArithmeticError("算力券换算快照必须包含配置版本")
        _require_integer(self.schema_version, "换算规则版本")
        if self.schema_version <= 0:
            raise TicketArithmeticError("换算规则版本必须为正整数")
        rate = _require_integer(self.tokens_per_ticket, "每张算力券 token 数")
        if rate <= 0:
            raise TicketArithmeticError("每张算力券 token 数必须为正整数")
        if rate > MAX_BIGINT:
            raise TicketArithmeticError("每张算力券 token 数超出可存储范围")
        object.__setattr__(self, "config_version_id", self.config_version_id.strip())

    def convert(self, metering_tokens: int) -> int:
        return metering_tokens_to_microtickets(
            metering_tokens,
            self.tokens_per_ticket,
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            "config_version_id": self.config_version_id,
            "tokens_per_ticket": self.tokens_per_ticket,
            "schema_version": self.schema_version,
        }
