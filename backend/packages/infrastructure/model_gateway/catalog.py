"""Trusted, versioned price catalog boundaries for post-call cost attribution."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    SUPPORTED_COST_CURRENCIES,
    CostAttribution,
    ModelRoute,
    PriceCatalogRate,
    ProviderUsage,
    calculate_catalog_cost,
)


@dataclass(frozen=True, slots=True)
class PriceCatalogVersion:
    id: UUID
    provider: str
    model: str
    currency: str
    effective_from: datetime
    effective_to: datetime | None
    source_ref: str
    source_digest: str
    content_hash: str
    rates: tuple[PriceCatalogRate, ...]
    status: str = "PUBLISHED"

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("price catalog provider and model are required")
        if self.effective_from.tzinfo is None:
            raise ValueError("price catalog effective_from must be timezone-aware")
        if self.effective_to is not None:
            if self.effective_to.tzinfo is None:
                raise ValueError("price catalog effective_to must be timezone-aware")
            if self.effective_to <= self.effective_from:
                raise ValueError("price catalog effective range must be positive")
        if len(self.content_hash) != 71 or not self.content_hash.startswith("sha256:"):
            raise ValueError("price catalog content_hash must be sha256-prefixed")
        if self.currency not in SUPPORTED_COST_CURRENCIES:
            raise ValueError("price catalog currency must be USD or CNY")
        if self.status not in {"DRAFT", "PUBLISHED"}:
            raise ValueError("price catalog status is invalid")
        if len(self.source_digest) != 71 or not self.source_digest.startswith(
            "sha256:"
        ):
            raise ValueError("price catalog source_digest must be sha256-prefixed")
        if not self.rates:
            raise ValueError("price catalog requires at least one rate")

    def applies_at(self, occurred_at: datetime) -> bool:
        if occurred_at.tzinfo is None:
            raise ValueError("price catalog lookup time must be timezone-aware")
        return self.effective_from <= occurred_at and (
            self.effective_to is None or occurred_at < self.effective_to
        )


class PriceCatalogReader:
    async def get_catalog(
        self,
        context: TenantContext,
        *,
        provider: str,
        model: str,
        occurred_at: datetime,
    ) -> PriceCatalogVersion | None:
        raise NotImplementedError


class InMemoryPriceCatalogReader(PriceCatalogReader):
    """Explicit test/local adapter; production must provide a trusted store."""

    def __init__(self, catalogs: Iterable[PriceCatalogVersion] = ()) -> None:
        self._catalogs = tuple(catalogs)

    async def get_catalog(
        self,
        context: TenantContext,
        *,
        provider: str,
        model: str,
        occurred_at: datetime,
    ) -> PriceCatalogVersion | None:
        del context
        matches = [
            catalog
            for catalog in self._catalogs
            if catalog.provider == provider
            and catalog.model == model
            and catalog.status == "PUBLISHED"
            and catalog.applies_at(occurred_at)
        ]
        if not matches:
            return None
        return max(matches, key=lambda catalog: catalog.effective_from)


class InMemoryPriceCatalogPublisher:
    """Controlled draft/publish/rollback boundary for tests and local operation."""

    def __init__(self) -> None:
        self._catalogs: dict[UUID, PriceCatalogVersion] = {}

    def create_draft(self, catalog: PriceCatalogVersion) -> PriceCatalogVersion:
        if catalog.status != "DRAFT" or catalog.id in self._catalogs:
            raise ValueError("price catalog draft is invalid or already exists")
        self._catalogs[catalog.id] = catalog
        return catalog

    def publish(self, catalog_id: UUID) -> PriceCatalogVersion:
        from dataclasses import replace

        draft = self._catalogs[catalog_id]
        if draft.status != "DRAFT":
            raise ValueError("only a draft price catalog can be published")
        for existing in self._catalogs.values():
            if (
                existing.status == "PUBLISHED"
                and existing.provider == draft.provider
                and existing.model == draft.model
                and _ranges_overlap(existing, draft)
            ):
                raise ValueError("published price catalog effective ranges overlap")
        published = replace(draft, status="PUBLISHED")
        self._catalogs[catalog_id] = published
        return published

    def rollback(self, source_id: UUID, *, new_id: UUID) -> PriceCatalogVersion:
        from dataclasses import replace

        source = self._catalogs[source_id]
        if source.status != "PUBLISHED" or new_id in self._catalogs:
            raise ValueError("rollback source or target is invalid")
        draft = replace(source, id=new_id, status="DRAFT")
        self._catalogs[new_id] = draft
        return draft


def _ranges_overlap(left: PriceCatalogVersion, right: PriceCatalogVersion) -> bool:
    return (left.effective_to is None or right.effective_from < left.effective_to) and (
        right.effective_to is None or left.effective_from < right.effective_to
    )


class CatalogCostAttributor:
    def __init__(self, reader: PriceCatalogReader) -> None:
        self._reader = reader

    async def attribute(
        self,
        context: TenantContext,
        route: ModelRoute,
        usage: ProviderUsage | None,
        occurred_at: datetime,
    ) -> CostAttribution:
        if usage is None:
            return CostAttribution()
        if usage.cost is not None:
            return CostAttribution(cost=usage.cost, source="PROVIDER_REPORTED")
        catalog = await self._reader.get_catalog(
            context,
            provider=route.provider,
            model=route.model,
            occurred_at=occurred_at,
        )
        if catalog is None:
            return CostAttribution()
        result = calculate_catalog_cost(
            usage, currency=catalog.currency, rates=catalog.rates
        )
        if result is None:
            return CostAttribution()
        cost, components = result
        used_rate_ids = frozenset(str(component["rate_id"]) for component in components)
        return CostAttribution(
            cost=cost,
            source="CATALOG_CALCULATED",
            price_catalog_version_id=catalog.id,
            price_catalog_rate_ids=tuple(
                rate.id for rate in catalog.rates if str(rate.id) in used_rate_ids
            ),
            details={
                "source_ref": catalog.source_ref,
                "content_hash": catalog.content_hash,
                "components": components,
            },
        )
