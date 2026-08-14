"""Provider-neutral Model Gateway domain values."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from packages.contracts.model_gateway import Capability, Money

ProviderSubmissionState = Literal["not_submitted", "submitted", "unknown"]
ProviderFinishReason = Literal["stop", "length", "tool_call", "cancelled", "unknown"]
ModelParameterValue = str | float | int | bool | None
PriceDimension = Literal[
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
]
CostSource = Literal["PROVIDER_REPORTED", "CATALOG_CALCULATED"]
SupportedCurrency = Literal["USD", "CNY"]

_COST_QUANTUM = Decimal("0.00000001")
SUPPORTED_COST_CURRENCIES = frozenset({"USD", "CNY"})


def _empty_model_parameters() -> Mapping[str, ModelParameterValue]:
    return MappingProxyType({})


def _empty_fallback_error_codes() -> frozenset[str]:
    return frozenset()


def _empty_object_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


_NON_FALLBACK_ERROR_CODES = frozenset(
    {
        "BUDGET_GUARD_UNAVAILABLE",
        "COST_BUDGET_UNAVAILABLE",
        "GATEWAY_RATE_LIMITED",
        "RATE_LIMITER_UNAVAILABLE",
        "TOKEN_BUDGET_EXCEEDED",
    }
)


class ProviderError(RuntimeError):
    """Safe provider failure metadata used for retry and fallback decisions."""

    code: str
    safe_message: str
    retryable: bool
    submission_state: ProviderSubmissionState
    provider_code: str | None

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        submission_state: ProviderSubmissionState,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.submission_state = submission_state
        self.provider_code = provider_code

    @property
    def allows_fallback(self) -> bool:
        return self.retryable and self.submission_state == "not_submitted"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost: Money | None = None


@dataclass(frozen=True, slots=True)
class PriceCatalogRate:
    id: UUID
    dimension: PriceDimension
    unit_tokens: int
    unit_price: Decimal

    def __post_init__(self) -> None:
        if self.unit_tokens < 1:
            raise ValueError("price catalog unit_tokens must be positive")
        if self.unit_price < 0:
            raise ValueError("price catalog unit_price cannot be negative")


@dataclass(frozen=True, slots=True)
class CostAttribution:
    cost: Money | None = None
    source: CostSource | None = None
    price_catalog_version_id: UUID | None = None
    price_catalog_rate_ids: tuple[UUID, ...] = ()
    details: Mapping[str, object] = field(default_factory=_empty_object_mapping)

    def __post_init__(self) -> None:
        details: dict[str, object] = dict(self.details)
        object.__setattr__(self, "details", MappingProxyType(details))
        if self.cost is None:
            if (
                self.source is not None
                or self.price_catalog_version_id is not None
                or self.price_catalog_rate_ids
                or self.details
            ):
                raise ValueError("missing cost cannot carry attribution provenance")
            return
        if self.source is None:
            raise ValueError("attributed cost requires a source")
        if self.source == "CATALOG_CALCULATED":
            if self.price_catalog_version_id is None:
                raise ValueError("catalog cost requires a catalog version")
            if not self.price_catalog_rate_ids:
                raise ValueError("catalog cost requires at least one rate")
        elif self.price_catalog_version_id is not None or self.price_catalog_rate_ids:
            raise ValueError("provider-reported cost cannot reference catalog rates")


def calculate_catalog_cost(
    usage: ProviderUsage,
    *,
    currency: str,
    rates: tuple[PriceCatalogRate, ...],
) -> tuple[Money, tuple[dict[str, object], ...]] | None:
    """Calculate an auditable post-call cost, failing closed on missing rates."""

    rate_by_dimension: dict[PriceDimension, PriceCatalogRate] = {}
    for rate in rates:
        if rate.dimension in rate_by_dimension:
            raise ValueError("price catalog contains duplicate dimensions")
        rate_by_dimension[rate.dimension] = rate

    token_counts: dict[PriceDimension, int | None] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }
    components: list[dict[str, object]] = []
    total = Decimal(0)
    for dimension, token_count in token_counts.items():
        if token_count is None:
            return None
        if token_count < 0:
            raise ValueError("usage token counts cannot be negative")
        if token_count == 0:
            continue
        rate = rate_by_dimension.get(dimension)
        if rate is None:
            return None
        amount = (
            Decimal(token_count) * rate.unit_price / Decimal(rate.unit_tokens)
        ).quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)
        total += amount
        components.append(
            {
                "rate_id": str(rate.id),
                "dimension": dimension,
                "tokens": token_count,
                "unit_tokens": rate.unit_tokens,
                "unit_price": format(rate.unit_price, "f"),
                "amount": format(amount, "f"),
            }
        )
    if not components:
        return None
    total = total.quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)
    return Money(amount=format(total, "f"), currency=currency), tuple(components)


def calculate_catalog_upper_bound(
    token_counts: Mapping[PriceDimension, int],
    *,
    currency: str,
    rates: tuple[PriceCatalogRate, ...],
) -> Money:
    """Calculate a conservative per-route upper bound, ceiling-rounded to 1e-8."""

    if currency not in SUPPORTED_COST_CURRENCIES:
        raise ValueError("cost currency must be USD or CNY")
    rate_by_dimension = {rate.dimension: rate for rate in rates}
    if len(rate_by_dimension) != len(rates):
        raise ValueError("price catalog contains duplicate dimensions")
    total = Decimal(0)
    for dimension, token_count in token_counts.items():
        if token_count < 0:
            raise ValueError("upper-bound token counts cannot be negative")
        if token_count == 0:
            continue
        rate = rate_by_dimension.get(dimension)
        if rate is None:
            raise ValueError(f"price catalog rate is missing for {dimension}")
        total += Decimal(token_count) * rate.unit_price / Decimal(rate.unit_tokens)
    return Money(
        amount=format(total.quantize(_COST_QUANTUM, rounding=ROUND_CEILING), "f"),
        currency=currency,
    )


@dataclass(frozen=True, slots=True)
class ModelRoute:
    provider: str
    model: str
    base_url: str
    secret_ref: str
    capabilities: frozenset[Capability]
    timeout_seconds: int
    default_parameters: Mapping[str, ModelParameterValue] = field(
        default_factory=_empty_model_parameters
    )
    max_context_tokens: int | None = None
    rate_limit_rpm: int | None = None
    max_output_tokens: int | None = None
    max_reasoning_tokens: int | None = None
    counter_profile_id: str | None = None
    counter_profile_version: str | None = None
    counter_profile_hash: str | None = None
    billing_semantics_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_parameters",
            MappingProxyType(dict(self.default_parameters)),
        )
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.max_reasoning_tokens is not None and self.max_reasoning_tokens < 1:
            raise ValueError("max_reasoning_tokens must be positive")
        counter_values = (
            self.counter_profile_id,
            self.counter_profile_version,
            self.counter_profile_hash,
        )
        if any(value is not None for value in counter_values) and not all(
            value is not None for value in counter_values
        ):
            raise ValueError("counter profile identity, version and hash are atomic")
        if self.counter_profile_hash is not None and (
            len(self.counter_profile_hash) != 71
            or not self.counter_profile_hash.startswith("sha256:")
        ):
            raise ValueError("counter profile hash must be sha256-prefixed")
        if self.billing_semantics_version is not None and not all(
            value is not None for value in counter_values
        ):
            raise ValueError("billing semantics require a complete counter profile")


@dataclass(frozen=True, slots=True)
class ModelBinding:
    binding_id: str
    routes: tuple[ModelRoute, ...]
    max_fallbacks: int = 0
    fallback_error_codes: frozenset[str] = field(
        default_factory=_empty_fallback_error_codes
    )

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("model binding requires at least one route")
        if len(self.routes) > 3:
            raise ValueError(
                "model binding supports one primary and at most two fallbacks"
            )
        if self.max_fallbacks < 0 or self.max_fallbacks > 2:
            raise ValueError("max_fallbacks must be between zero and two")
        if self.max_fallbacks > len(self.routes) - 1:
            raise ValueError("max_fallbacks exceeds the configured fallback routes")

    def allows_fallback(self, error: ProviderError, route_index: int) -> bool:
        return (
            error.allows_fallback
            and error.code not in _NON_FALLBACK_ERROR_CODES
            and error.code in self.fallback_error_codes
            and route_index < self.max_fallbacks
            and route_index + 1 < len(self.routes)
        )


@dataclass(frozen=True, slots=True)
class ProviderConnectionTarget:
    provider_id: UUID
    provider: str
    base_url: str
    secret_ref: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ModelInvocationInput:
    messages: tuple[Mapping[str, JsonValue], ...]
    tools: tuple[Mapping[str, JsonValue], ...] = ()

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("model invocation requires at least one message")
        object.__setattr__(
            self,
            "messages",
            tuple(MappingProxyType(dict(message)) for message in self.messages),
        )
        object.__setattr__(
            self,
            "tools",
            tuple(MappingProxyType(dict(tool)) for tool in self.tools),
        )


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    provider_request_id: str | None
    finish_reason: ProviderFinishReason
    output: JsonValue
    usage: ProviderUsage | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterStreamStarted:
    provider_request_id: str | None


@dataclass(frozen=True, slots=True)
class AdapterTextDelta:
    delta: str
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterToolCallDelta:
    tool_call_id: str
    arguments_patch: str
    tool_name: str | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterUsageEvent:
    usage: ProviderUsage
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterStreamCompleted:
    finish_reason: ProviderFinishReason
    output: JsonValue
    provider_request_id: str | None = None


type AdapterStreamEvent = (
    AdapterStreamStarted
    | AdapterTextDelta
    | AdapterToolCallDelta
    | AdapterUsageEvent
    | AdapterStreamCompleted
)


@dataclass(frozen=True, slots=True)
class ModelUsageRecord:
    id: UUID
    tenant_id: UUID
    run_id: str
    provider: str
    model: str
    provider_request_id: str | None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    token_estimated: bool
    cost_amount: str | None
    cost_currency: str | None
    started_at: datetime
    finished_at: datetime
    cost_source: CostSource | None = None
    price_catalog_version_id: UUID | None = None
    cost_details: Mapping[str, object] = field(default_factory=_empty_object_mapping)

    def __post_init__(self) -> None:
        details: dict[str, object] = dict(self.cost_details)
        object.__setattr__(self, "cost_details", MappingProxyType(details))
