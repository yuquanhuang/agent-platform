"""Provider-neutral Model Gateway domain values."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from packages.contracts.model_gateway import Capability, Money

ProviderSubmissionState = Literal["not_submitted", "submitted", "unknown"]
ProviderFinishReason = Literal["stop", "length", "tool_call", "cancelled", "unknown"]
ModelParameterValue = str | float | int | bool | None


def _empty_model_parameters() -> Mapping[str, ModelParameterValue]:
    return MappingProxyType({})


def _empty_fallback_error_codes() -> frozenset[str]:
    return frozenset()


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_parameters",
            MappingProxyType(dict(self.default_parameters)),
        )


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
