"""Trusted price catalog selection and attribution tests."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from packages.contracts.model_gateway import Money
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import ModelRoute, PriceCatalogRate, ProviderUsage
from packages.infrastructure.model_gateway import (
    CatalogCostAttributor,
    InMemoryPriceCatalogPublisher,
    InMemoryPriceCatalogReader,
    PriceCatalogVersion,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
CATALOG_ID = UUID("33333333-3333-4333-8333-333333333333")
RATE_ID = UUID("44444444-4444-4444-8444-444444444444")


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR_ID),
        membership_version=1,
        auth_time=datetime(2026, 8, 12, tzinfo=UTC),
        request_id="req-catalog",
        trace_id="trace-catalog",
    )


def route() -> ModelRoute:
    return ModelRoute(
        provider="openai",
        model="test-only-model",
        base_url="https://provider.test/v1",
        secret_ref="secret://tenant/model",
        capabilities=frozenset(),
        timeout_seconds=5,
    )


def catalog(*, effective_from: datetime) -> PriceCatalogVersion:
    return PriceCatalogVersion(
        id=CATALOG_ID,
        provider="openai",
        model="test-only-model",
        currency="USD",
        effective_from=effective_from,
        effective_to=None,
        source_ref="test://trusted-price-fixture",
        source_digest="sha256:" + "b" * 64,
        content_hash="sha256:" + "a" * 64,
        rates=(
            PriceCatalogRate(
                id=RATE_ID,
                dimension="input_tokens",
                unit_tokens=1000,
                unit_price=Decimal("0.100000000000"),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_catalog_attributor_selects_effective_version_and_records_provenance() -> (
    None
):
    occurred_at = datetime(2026, 8, 12, 12, tzinfo=UTC)
    attributor = CatalogCostAttributor(
        InMemoryPriceCatalogReader(
            (catalog(effective_from=datetime(2026, 8, 1, tzinfo=UTC)),)
        )
    )

    result = await attributor.attribute(
        context(),
        route(),
        ProviderUsage(
            input_tokens=100,
            output_tokens=0,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        occurred_at,
    )

    assert result.cost == Money(amount="0.01000000", currency="USD")
    assert result.source == "CATALOG_CALCULATED"
    assert result.price_catalog_version_id == CATALOG_ID
    assert result.price_catalog_rate_ids == (RATE_ID,)
    assert result.details["source_ref"] == "test://trusted-price-fixture"


@pytest.mark.asyncio
async def test_catalog_attributor_keeps_unknown_price_unattributed() -> None:
    result = await CatalogCostAttributor(InMemoryPriceCatalogReader()).attribute(
        context(),
        route(),
        ProviderUsage(
            input_tokens=1,
            output_tokens=0,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert result.cost is None
    assert result.source is None


@pytest.mark.asyncio
async def test_provider_reported_cost_has_priority_over_catalog() -> None:
    result = await CatalogCostAttributor(InMemoryPriceCatalogReader()).attribute(
        context(),
        route(),
        ProviderUsage(cost=Money(amount="1.25", currency="USD")),
        datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert result.cost == Money(amount="1.25", currency="USD")
    assert result.source == "PROVIDER_REPORTED"
    assert result.price_catalog_version_id is None


def test_catalog_publication_rejects_overlap_and_rollback_creates_new_draft() -> None:
    publisher = InMemoryPriceCatalogPublisher()
    original = catalog(effective_from=datetime(2026, 8, 1, tzinfo=UTC))
    publisher.create_draft(replace(original, status="DRAFT"))
    assert publisher.publish(original.id).status == "PUBLISHED"
    rolled_back = publisher.rollback(
        original.id, new_id=UUID("55555555-5555-4555-8555-555555555555")
    )
    assert rolled_back.status == "DRAFT"
    assert rolled_back.id != original.id

    overlap = replace(
        original,
        id=UUID("66666666-6666-4666-8666-666666666666"),
        status="DRAFT",
    )
    publisher.create_draft(overlap)
    with pytest.raises(ValueError, match="overlap"):
        publisher.publish(overlap.id)
