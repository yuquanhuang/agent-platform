"""Post-call model cost calculation without production supplier prices."""

from decimal import Decimal
from uuid import UUID

import pytest

from packages.domain.model_gateway import (
    PriceCatalogRate,
    ProviderUsage,
    calculate_catalog_cost,
)

INPUT_RATE = PriceCatalogRate(
    id=UUID("11111111-1111-4111-8111-111111111111"),
    dimension="input_tokens",
    unit_tokens=1000,
    unit_price=Decimal("0.100000000000"),
)
OUTPUT_RATE = PriceCatalogRate(
    id=UUID("22222222-2222-4222-8222-222222222222"),
    dimension="output_tokens",
    unit_tokens=1000,
    unit_price=Decimal("0.200000000000"),
)


def test_calculate_catalog_cost_uses_decimal_and_eight_decimal_money() -> None:
    result = calculate_catalog_cost(
        ProviderUsage(
            input_tokens=1500,
            output_tokens=250,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        currency="USD",
        rates=(INPUT_RATE, OUTPUT_RATE),
    )

    assert result is not None
    cost, components = result
    assert cost.amount == "0.20000000"
    assert cost.currency == "USD"
    assert [component["dimension"] for component in components] == [
        "input_tokens",
        "output_tokens",
    ]


def test_calculate_catalog_cost_fails_closed_for_missing_usage_or_rate() -> None:
    assert (
        calculate_catalog_cost(
            ProviderUsage(input_tokens=10, output_tokens=None),
            currency="USD",
            rates=(INPUT_RATE,),
        )
        is None
    )
    assert (
        calculate_catalog_cost(
            ProviderUsage(
                input_tokens=10,
                output_tokens=1,
                reasoning_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            currency="USD",
            rates=(INPUT_RATE,),
        )
        is None
    )


def test_calculate_catalog_cost_rejects_duplicate_dimensions() -> None:
    with pytest.raises(ValueError, match="duplicate dimensions"):
        calculate_catalog_cost(
            ProviderUsage(
                input_tokens=10,
                output_tokens=0,
                reasoning_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            currency="USD",
            rates=(INPUT_RATE, INPUT_RATE),
        )
