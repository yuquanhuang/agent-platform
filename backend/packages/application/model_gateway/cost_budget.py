"""Trusted pre-call model cost-bound planning boundaries."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID

from packages.contracts.model_gateway import Money
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelInvocationInput,
    PriceCatalogRate,
    PriceDimension,
    ProviderError,
    calculate_catalog_upper_bound,
)


class PriceCatalog(Protocol):
    id: UUID
    provider: str
    model: str
    currency: str
    status: str
    rates: tuple[PriceCatalogRate, ...]


class PriceCatalogReader(Protocol):
    async def get_catalog(
        self,
        context: TenantContext,
        *,
        provider: str,
        model: str,
        occurred_at: datetime,
    ) -> PriceCatalog | None: ...


@dataclass(frozen=True, slots=True)
class CountedInput:
    canonical_input_hash: str
    counter_profile_id: str
    counter_profile_version: str
    counter_profile_hash: str
    token_counts: Mapping[PriceDimension, int]


class TrustedTokenCounter(Protocol):
    async def count(
        self,
        *,
        invocation: ModelInvocationInput,
        counter_profile_id: str,
        counter_profile_version: str,
        counter_profile_hash: str,
    ) -> CountedInput: ...


class DeterministicJsonTokenCounter:
    """Deterministic test counter; production providers must inject pinned counters."""

    async def count(
        self,
        *,
        invocation: ModelInvocationInput,
        counter_profile_id: str,
        counter_profile_version: str,
        counter_profile_hash: str,
    ) -> CountedInput:
        canonical = json.dumps(
            {
                "messages": [dict(value) for value in invocation.messages],
                "tools": [dict(value) for value in invocation.tools],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return CountedInput(
            canonical_input_hash="sha256:" + hashlib.sha256(canonical).hexdigest(),
            counter_profile_id=counter_profile_id,
            counter_profile_version=counter_profile_version,
            counter_profile_hash=counter_profile_hash,
            token_counts={"input_tokens": len(canonical)},
        )


@dataclass(frozen=True, slots=True)
class RouteCostBound:
    route_index: int
    provider: str
    model: str
    catalog_version_id: UUID
    amount: Money


@dataclass(frozen=True, slots=True)
class CostBoundPlan:
    counted_input: CountedInput
    amount: Money
    routes: tuple[RouteCostBound, ...]


class CostBoundPlanner:
    def __init__(self, *, counter: TrustedTokenCounter, catalogs: object) -> None:
        self._counter = counter
        self._catalogs = cast(PriceCatalogReader, catalogs)

    async def plan(
        self,
        context: TenantContext,
        *,
        binding: ModelBinding,
        invocation: ModelInvocationInput,
        currency: str,
        occurred_at: datetime,
    ) -> CostBoundPlan:
        routes: list[RouteCostBound] = []
        counted: CountedInput | None = None
        for route_index, route in enumerate(binding.routes):
            if (
                route.max_output_tokens is None
                or route.counter_profile_id is None
                or route.counter_profile_version is None
                or route.counter_profile_hash is None
                or route.billing_semantics_version is None
            ):
                raise _unavailable()
            if route.max_reasoning_tokens is not None and route.provider != "qwen":
                raise _unavailable()
            current_count = await self._counter.count(
                invocation=invocation,
                counter_profile_id=route.counter_profile_id,
                counter_profile_version=route.counter_profile_version,
                counter_profile_hash=route.counter_profile_hash,
            )
            if counted is None:
                counted = current_count
            elif counted.canonical_input_hash != current_count.canonical_input_hash:
                raise _unavailable()
            catalog = await self._catalogs.get_catalog(
                context,
                provider=route.provider,
                model=route.model,
                occurred_at=occurred_at,
            )
            if catalog is None or catalog.status != "PUBLISHED":
                raise _unavailable()
            _require_currency(catalog, currency)
            token_counts: dict[PriceDimension, int] = dict(current_count.token_counts)
            token_counts["output_tokens"] = route.max_output_tokens
            token_counts["reasoning_tokens"] = route.max_reasoning_tokens or 0
            amount = calculate_catalog_upper_bound(
                token_counts, currency=currency, rates=catalog.rates
            )
            routes.append(
                RouteCostBound(
                    route_index=route_index,
                    provider=route.provider,
                    model=route.model,
                    catalog_version_id=catalog.id,
                    amount=amount,
                )
            )
        if counted is None or not routes:
            raise _unavailable()
        maximum = max(routes, key=lambda value: _amount(value.amount))
        return CostBoundPlan(
            counted_input=counted, amount=maximum.amount, routes=tuple(routes)
        )


def _require_currency(catalog: PriceCatalog, currency: str) -> None:
    if currency not in {"USD", "CNY"} or catalog.currency != currency:
        raise ProviderError(
            code="COST_CURRENCY_MISMATCH",
            message="The request, policy and price catalog currencies must match.",
            retryable=False,
            submission_state="not_submitted",
        )


def _amount(value: Money) -> Decimal:
    return Decimal(value.amount)


def _unavailable() -> ProviderError:
    return ProviderError(
        code="COST_BOUND_UNAVAILABLE",
        message="A trusted model cost upper bound is unavailable.",
        retryable=False,
        submission_state="not_submitted",
    )
