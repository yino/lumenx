from __future__ import annotations

import pytest

from src.platform.ticket_math import (
    MAX_BIGINT,
    MICROTICKETS_PER_TICKET,
    TicketArithmeticError,
    TicketConversionSnapshot,
    add_microtickets,
    metering_tokens_to_microtickets,
    subtract_microtickets,
)


@pytest.mark.parametrize(
    ("metering_tokens", "tokens_per_ticket", "expected"),
    [
        (0, 1000, 0),
        (1000, 1000, MICROTICKETS_PER_TICKET),
        (1, 1000, 1000),
        (1, 3, 333_334),
        (2, 3, 666_667),
        (3, 3, MICROTICKETS_PER_TICKET),
    ],
)
def test_metering_token_conversion_uses_integer_ceiling(
    metering_tokens: int,
    tokens_per_ticket: int,
    expected: int,
) -> None:
    assert metering_tokens_to_microtickets(metering_tokens, tokens_per_ticket) == expected


def test_conversion_snapshot_keeps_versioned_exchange_rate() -> None:
    original = TicketConversionSnapshot(
        config_version_id="config-v1",
        tokens_per_ticket=1000,
    )
    replacement = TicketConversionSnapshot(
        config_version_id="config-v2",
        tokens_per_ticket=2000,
    )

    assert original.convert(750) == 750_000
    assert replacement.convert(750) == 375_000
    assert original.as_dict() == {
        "config_version_id": "config-v1",
        "tokens_per_ticket": 1000,
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    ("metering_tokens", "tokens_per_ticket"),
    [
        (-1, 1000),
        (1, 0),
        (1, -1),
        (True, 1000),
        (1, True),
        (MAX_BIGINT + 1, 1000),
        (1, MAX_BIGINT + 1),
    ],
)
def test_conversion_rejects_invalid_integer_boundaries(
    metering_tokens: int,
    tokens_per_ticket: int,
) -> None:
    with pytest.raises(TicketArithmeticError):
        metering_tokens_to_microtickets(metering_tokens, tokens_per_ticket)


def test_conversion_rejects_bigint_result_overflow() -> None:
    largest_safe_token_count = MAX_BIGINT // MICROTICKETS_PER_TICKET

    assert (
        metering_tokens_to_microtickets(largest_safe_token_count, 1)
        <= MAX_BIGINT
    )
    with pytest.raises(TicketArithmeticError, match="超出可存储范围"):
        metering_tokens_to_microtickets(largest_safe_token_count + 1, 1)


def test_microticket_addition_and_subtraction_enforce_wallet_boundaries() -> None:
    assert add_microtickets(2_000_000, 500_000) == 2_500_000
    assert subtract_microtickets(2_500_000, 500_000) == 2_000_000
    assert add_microtickets(MAX_BIGINT, 0) == MAX_BIGINT

    with pytest.raises(TicketArithmeticError, match="超出可存储范围"):
        add_microtickets(MAX_BIGINT, 1)
    with pytest.raises(TicketArithmeticError, match="余额不足"):
        subtract_microtickets(0, 1)


def test_conversion_snapshot_requires_valid_version_metadata() -> None:
    with pytest.raises(TicketArithmeticError, match="配置版本"):
        TicketConversionSnapshot(config_version_id=" ", tokens_per_ticket=1000)
    with pytest.raises(TicketArithmeticError, match="规则版本"):
        TicketConversionSnapshot(
            config_version_id="config-v1",
            tokens_per_ticket=1000,
            schema_version=0,
        )


def test_conversion_ceiling_property_across_integer_boundaries() -> None:
    for tokens_per_ticket in [1, 2, 3, 7, 10, 999, 1000, 4096, 1_000_003]:
        for metering_tokens in [0, 1, 2, 3, 6, 7, 8, 999, 1000, 4095, 65_537]:
            converted = metering_tokens_to_microtickets(
                metering_tokens,
                tokens_per_ticket,
            )
            numerator = metering_tokens * MICROTICKETS_PER_TICKET

            assert converted * tokens_per_ticket >= numerator
            if converted > 0:
                assert (converted - 1) * tokens_per_ticket < numerator
