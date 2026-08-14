"""Trusted cost upper-bound planning tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from packages.application.model_gateway.cost_budget import (
    CostBoundPlanner,
    DeterministicJsonTokenCounter,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelInvocationInput,
    ModelRoute,
    PriceCatalogRate,
    ProviderError,
)
from packages.infrastructure.model_gateway.catalog import (
    InMemoryPriceCatalogReader,
    PriceCatalogVersion,
)

HASH = "sha256:" + "a" * 64


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="11111111-1111-4111-8111-111111111111",
        subject_type=SubjectType.SERVICE,
        subject_id="22222222-2222-4222-8222-222222222222",
        request_id="req-cost",
        trace_id="trace-cost",
        auth_time=datetime(2026, 8, 13, tzinfo=UTC),
    )


def _route(provider: str, output: int) -> ModelRoute:
    return ModelRoute(
        provider=provider,
        model="test-model",
        base_url="https://provider.test/v1",
        secret_ref="secret://test",
        capabilities=frozenset(),
        timeout_seconds=5,
        max_output_tokens=output,
        counter_profile_id="test-json",
        counter_profile_version="1",
        counter_profile_hash=HASH,
        billing_semantics_version="1",
    )


def _catalog(provider: str, price: str) -> PriceCatalogVersion:
    return PriceCatalogVersion(
        id=(
            UUID("33333333-3333-4333-8333-333333333333")
            if provider == "openai"
            else UUID("44444444-4444-4444-8444-444444444444")
        ),
        provider=provider,
        model="test-model",
        currency="USD",
        effective_from=datetime(2026, 8, 1, tzinfo=UTC),
        effective_to=None,
        source_ref="test://price",
        source_digest=HASH,
        content_hash=HASH,
        rates=(
            PriceCatalogRate(
                id=UUID(int=1),
                dimension="input_tokens",
                unit_tokens=1000,
                unit_price=Decimal(price),
            ),
            PriceCatalogRate(
                id=UUID(int=2),
                dimension="output_tokens",
                unit_tokens=1000,
                unit_price=Decimal(price),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plan_reserves_maximum_fallback_route_bound() -> None:
    planner = CostBoundPlanner(
        counter=DeterministicJsonTokenCounter(),
        catalogs=InMemoryPriceCatalogReader(
            (_catalog("openai", "1"), _catalog("qwen", "2"))
        ),
    )
    plan = await planner.plan(
        _context(),
        binding=ModelBinding(
            binding_id="binding", routes=(_route("openai", 10), _route("qwen", 20))
        ),
        invocation=ModelInvocationInput(
            messages=({"role": "user", "content": "hello"},)
        ),
        currency="USD",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    assert plan.amount.currency == "USD"
    assert Decimal(plan.amount.amount) == max(
        Decimal(route.amount.amount) for route in plan.routes
    )
    assert plan.counted_input.canonical_input_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_plan_fails_closed_without_frozen_counter_or_cap() -> None:
    planner = CostBoundPlanner(
        counter=DeterministicJsonTokenCounter(), catalogs=InMemoryPriceCatalogReader()
    )
    with pytest.raises(ProviderError) as error:
        await planner.plan(
            _context(),
            binding=ModelBinding(
                binding_id="binding",
                routes=(
                    _route("openai", 10).__class__(
                        provider="openai",
                        model="test-model",
                        base_url="https://provider.test/v1",
                        secret_ref="secret://test",
                        capabilities=frozenset(),
                        timeout_seconds=5,
                    ),
                ),
            ),
            invocation=ModelInvocationInput(
                messages=({"role": "user", "content": "hello"},)
            ),
            currency="USD",
            occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
    assert error.value.code == "COST_BOUND_UNAVAILABLE"
