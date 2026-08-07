"""Provider-neutral Model Gateway orchestration."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue, SecretStr

from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayStreamEvent,
    ModelUsage,
    ResponseCompletedEvent,
    ResponseCompletedPayload,
    ResponseErrorEvent,
    ResponseErrorPayload,
    ResponseStartedEvent,
    ResponseStartedPayload,
    TextDeltaEvent,
    TextDeltaPayload,
    ToolCallDeltaEvent,
    ToolCallDeltaPayload,
    UsageEvent,
    UsagePayload,
)
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    AdapterResponse,
    AdapterStreamEvent,
    AdapterStreamStarted,
    AdapterTextDelta,
    AdapterToolCallDelta,
    AdapterUsageEvent,
    ModelBinding,
    ModelInvocationInput,
    ModelRoute,
    ModelUsageRecord,
    ProviderConnectionTarget,
    ProviderError,
    ProviderUsage,
)


class ModelProviderAdapter(Protocol):
    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse: ...

    def stream(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AsyncIterator[AdapterStreamEvent]: ...

    async def test_connection(
        self, target: ProviderConnectionTarget, credential: SecretStr
    ) -> None: ...


class ModelBindingReader(Protocol):
    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding: ...


class ModelRequestMaterializer(Protocol):
    async def materialize(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> ModelInvocationInput: ...


class ModelOutputWriter(Protocol):
    async def write(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        output: JsonValue,
    ) -> ImmutableReference: ...


class SecretReferenceResolver(Protocol):
    async def resolve(self, context: TenantContext, secret_ref: str) -> SecretStr: ...


class BudgetGuard(Protocol):
    async def authorize(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
    ) -> "BudgetPermit": ...

    async def settle(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        permit: "BudgetPermit",
        usage: ModelUsage,
    ) -> ProviderError | None: ...

    async def release(self, context: TenantContext, permit: "BudgetPermit") -> None: ...


class ModelRateLimiter(Protocol):
    async def acquire(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
        route: ModelRoute,
    ) -> None: ...


class UsageRecorder(Protocol):
    async def record(self, context: TenantContext, usage: ModelUsageRecord) -> None: ...


class ModelGatewayObserver(Protocol):
    def observe_model_gateway_request(
        self, *, provider: str, mode: str, outcome: str
    ) -> None: ...

    def observe_model_gateway_fallback(
        self, *, source_provider: str, target_provider: str
    ) -> None: ...

    def observe_model_gateway_usage(
        self, *, provider: str, usage: ModelUsage
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BudgetPermit:
    reservation_id: UUID | None = None
    reserved_tokens: int | None = None


class NoopBudgetGuard:
    """Allow unbudgeted calls while failing closed for configured budgets."""

    async def authorize(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
    ) -> BudgetPermit:
        del context, binding
        if request.token_budget is not None or request.cost_budget is not None:
            raise ProviderError(
                code="BUDGET_GUARD_UNAVAILABLE",
                message="The model budget policy is not configured.",
                retryable=False,
                submission_state="not_submitted",
            )
        return BudgetPermit()

    async def settle(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        permit: BudgetPermit,
        usage: ModelUsage,
    ) -> ProviderError | None:
        del context, request, permit, usage
        return None

    async def release(self, context: TenantContext, permit: BudgetPermit) -> None:
        del context, permit


class NoopModelRateLimiter:
    async def acquire(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
        route: ModelRoute,
    ) -> None:
        del context, request, binding
        if route.rate_limit_rpm is not None:
            raise ProviderError(
                code="RATE_LIMITER_UNAVAILABLE",
                message="The model rate-limit policy is not configured.",
                retryable=False,
                submission_state="not_submitted",
            )


class NoopModelGatewayObserver:
    def observe_model_gateway_request(
        self, *, provider: str, mode: str, outcome: str
    ) -> None:
        del provider, mode, outcome

    def observe_model_gateway_fallback(
        self, *, source_provider: str, target_provider: str
    ) -> None:
        del source_provider, target_provider

    def observe_model_gateway_usage(self, *, provider: str, usage: ModelUsage) -> None:
        del provider, usage


class ProviderAdapterRegistry:
    def __init__(self, adapters: Mapping[str, ModelProviderAdapter]) -> None:
        self._adapters = dict(adapters)

    def get(self, provider: str) -> ModelProviderAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ProviderError(
                code="PROVIDER_UNSUPPORTED",
                message="The configured model provider is not available.",
                retryable=False,
                submission_state="not_submitted",
            )
        return adapter


def normalize_usage(usage: ProviderUsage | None) -> ModelUsage:
    values = (
        usage.input_tokens if usage is not None else None,
        usage.output_tokens if usage is not None else None,
        usage.reasoning_tokens if usage is not None else None,
        usage.cache_read_tokens if usage is not None else None,
        usage.cache_write_tokens if usage is not None else None,
    )
    if any(value is not None and value < 0 for value in values):
        raise ProviderError(
            code="PROVIDER_PROTOCOL_ERROR",
            message="The provider returned invalid usage metadata.",
            retryable=False,
            submission_state="submitted",
        )
    return ModelUsage(
        input_tokens=values[0] or 0,
        output_tokens=values[1] or 0,
        reasoning_tokens=values[2] or 0,
        cache_read_tokens=values[3] or 0,
        cache_write_tokens=values[4] or 0,
        estimated=usage is None or any(value is None for value in values),
        cost=usage.cost if usage is not None else None,
    )


class ModelGatewayService:
    def __init__(
        self,
        *,
        bindings: ModelBindingReader,
        materializer: ModelRequestMaterializer,
        output_writer: ModelOutputWriter,
        secrets: SecretReferenceResolver,
        adapters: ProviderAdapterRegistry,
        usage_recorder: UsageRecorder,
        budget_guard: BudgetGuard | None = None,
        rate_limiter: ModelRateLimiter | None = None,
        observer: ModelGatewayObserver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bindings = bindings
        self._materializer = materializer
        self._output_writer = output_writer
        self._secrets = secrets
        self._adapters = adapters
        self._usage_recorder = usage_recorder
        self._budget_guard = budget_guard or NoopBudgetGuard()
        self._rate_limiter = rate_limiter or NoopModelRateLimiter()
        self._observer = observer or NoopModelGatewayObserver()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def generate(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> ModelGatewayResponse:
        self._validate_request_context(context, request, expected_stream=False)
        binding = await self._bindings.get_binding(context, request.model_binding_id)
        for route in binding.routes:
            self._require_capabilities(route, request)
        invocation = await self._materialize(context, request)
        permit = await self._budget_guard.authorize(context, request, binding)
        last_error: ProviderError | None = None
        for fallback_count, route in enumerate(binding.routes):
            started_at = self._clock()
            try:
                await self._rate_limiter.acquire(context, request, binding, route)
                credential = await self._secrets.resolve(context, route.secret_ref)
                adapter = self._adapters.get(route.provider)
                timeout = min(request.timeout_seconds, route.timeout_seconds)
                response = await asyncio.wait_for(
                    adapter.generate(route, request, invocation, credential),
                    timeout=timeout,
                )
                output_ref = await self._write_output(context, request, response.output)
                usage = normalize_usage(response.usage)
                finished_at = self._clock()
                await self._record_usage(
                    context,
                    request,
                    route,
                    response.provider_request_id,
                    usage,
                    started_at,
                    finished_at,
                )
                budget_error = await self._budget_guard.settle(
                    context, request, permit, usage
                )
                if budget_error is not None:
                    raise budget_error
                self._observer.observe_model_gateway_request(
                    provider=route.provider, mode="generate", outcome="success"
                )
                return ModelGatewayResponse(
                    provider=route.provider,
                    model=route.model,
                    provider_request_id=response.provider_request_id,
                    finish_reason=response.finish_reason,
                    output_ref=output_ref,
                    usage=usage,
                    fallback_count=fallback_count,
                    warnings=list(response.warnings),
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                last_error = ProviderError(
                    code="PROVIDER_TIMEOUT",
                    message="The model provider timed out.",
                    retryable=False,
                    submission_state="unknown",
                )
                self._observer.observe_model_gateway_request(
                    provider=route.provider,
                    mode="generate",
                    outcome="error",
                )
                raise last_error from error
            except ProviderError as error:
                last_error = error
                if binding.allows_fallback(error, fallback_count):
                    self._observer.observe_model_gateway_fallback(
                        source_provider=route.provider,
                        target_provider=binding.routes[fallback_count + 1].provider,
                    )
                    continue
                if error.submission_state == "not_submitted":
                    await self._budget_guard.release(context, permit)
                self._observer.observe_model_gateway_request(
                    provider=route.provider,
                    mode="generate",
                    outcome=_request_error_outcome(error),
                )
                raise
            except (OSError, RuntimeError) as error:
                last_error = ProviderError(
                    code="PROVIDER_UNAVAILABLE",
                    message="The model provider request failed.",
                    retryable=False,
                    submission_state="unknown",
                )
                self._observer.observe_model_gateway_request(
                    provider=route.provider, mode="generate", outcome="error"
                )
                raise last_error from error
        if last_error is not None:
            raise last_error
        raise RuntimeError("model binding contains no routes")

    async def stream(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> AsyncIterator[ModelGatewayStreamEvent]:
        self._validate_request_context(context, request, expected_stream=True)
        binding = await self._bindings.get_binding(context, request.model_binding_id)
        for route in binding.routes:
            self._require_capabilities(route, request)
        invocation = await self._materialize(context, request)
        permit = await self._budget_guard.authorize(context, request, binding)
        for route_index, route in enumerate(binding.routes):
            started_at = self._clock()
            provider_request_id: str | None = None
            usage: ModelUsage | None = None
            emitted = False
            completed = False
            try:
                await self._rate_limiter.acquire(context, request, binding, route)
                credential = await self._secrets.resolve(context, route.secret_ref)
                adapter = self._adapters.get(route.provider)
                timeout = min(request.timeout_seconds, route.timeout_seconds)
                async with asyncio.timeout(timeout):
                    async for adapter_event in adapter.stream(
                        route, request, invocation, credential
                    ):
                        emitted = True
                        event, provider_request_id, event_usage, is_completed = (
                            await self._normalize_stream_event(
                                context,
                                request,
                                route,
                                adapter_event,
                                provider_request_id,
                            )
                        )
                        if event_usage is not None:
                            usage = event_usage
                        if is_completed:
                            if usage is None:
                                usage = normalize_usage(None)
                                yield self._usage_event(
                                    route, provider_request_id, usage
                                )
                            await self._record_usage(
                                context,
                                request,
                                route,
                                provider_request_id,
                                usage,
                                started_at,
                                self._clock(),
                            )
                            budget_error = await self._budget_guard.settle(
                                context, request, permit, usage
                            )
                            if budget_error is not None:
                                self._observer.observe_model_gateway_request(
                                    provider=route.provider,
                                    mode="stream",
                                    outcome="rejected",
                                )
                                yield self._error_event(
                                    route, provider_request_id, budget_error
                                )
                                return
                            completed = True
                            self._observer.observe_model_gateway_request(
                                provider=route.provider,
                                mode="stream",
                                outcome="success",
                            )
                        yield event
                if not completed:
                    raise ProviderError(
                        code="PROVIDER_PROTOCOL_ERROR",
                        message="The provider stream ended without a terminal event.",
                        retryable=False,
                        submission_state="submitted" if emitted else "unknown",
                    )
                return
            except asyncio.CancelledError:
                raise
            except ProviderError as error:
                if not emitted and binding.allows_fallback(error, route_index):
                    self._observer.observe_model_gateway_fallback(
                        source_provider=route.provider,
                        target_provider=binding.routes[route_index + 1].provider,
                    )
                    continue
                if error.submission_state == "not_submitted":
                    await self._budget_guard.release(context, permit)
                self._observer.observe_model_gateway_request(
                    provider=route.provider,
                    mode="stream",
                    outcome=_request_error_outcome(error),
                )
                yield self._error_event(route, provider_request_id, error)
                return
            except TimeoutError:
                error = ProviderError(
                    code="PROVIDER_TIMEOUT",
                    message="The model provider stream timed out.",
                    retryable=False,
                    submission_state="unknown" if not emitted else "submitted",
                )
                self._observer.observe_model_gateway_request(
                    provider=route.provider, mode="stream", outcome="error"
                )
                yield self._error_event(route, provider_request_id, error)
                return
            except (OSError, RuntimeError):
                error = ProviderError(
                    code="PROVIDER_UNAVAILABLE",
                    message="The model provider stream failed.",
                    retryable=False,
                    submission_state="unknown" if not emitted else "submitted",
                )
                self._observer.observe_model_gateway_request(
                    provider=route.provider, mode="stream", outcome="error"
                )
                yield self._error_event(route, provider_request_id, error)
                return

    @staticmethod
    def _validate_request_context(
        context: TenantContext,
        request: ModelGatewayRequest,
        *,
        expected_stream: bool,
    ) -> None:
        if request.tenant_id != context.tenant_id:
            raise ProviderError(
                code="TENANT_CONTEXT_MISMATCH",
                message="The request tenant does not match its authorization context.",
                retryable=False,
                submission_state="not_submitted",
            )
        if request.stream is not expected_stream:
            raise ProviderError(
                code="INVALID_REQUEST",
                message="The request stream mode does not match the gateway operation.",
                retryable=False,
                submission_state="not_submitted",
            )

    @staticmethod
    def _require_capabilities(route: ModelRoute, request: ModelGatewayRequest) -> None:
        if not set(request.capability_requirements).issubset(route.capabilities):
            raise ProviderError(
                code="CAPABILITY_UNSUPPORTED",
                message="The configured model does not satisfy required capabilities.",
                retryable=False,
                submission_state="not_submitted",
            )

    async def _record_usage(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        route: ModelRoute,
        provider_request_id: str | None,
        usage: ModelUsage,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        await self._usage_recorder.record(
            context,
            ModelUsageRecord(
                id=uuid4(),
                tenant_id=UUID(context.tenant_id),
                run_id=request.run_id,
                provider=route.provider,
                model=route.model,
                provider_request_id=provider_request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                token_estimated=usage.estimated,
                cost_amount=usage.cost.amount if usage.cost is not None else None,
                cost_currency=usage.cost.currency if usage.cost is not None else None,
                started_at=started_at,
                finished_at=finished_at,
            ),
        )
        self._observer.observe_model_gateway_usage(provider=route.provider, usage=usage)

    async def _normalize_stream_event(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        route: ModelRoute,
        event: AdapterStreamEvent,
        current_request_id: str | None,
    ) -> tuple[ModelGatewayStreamEvent, str | None, ModelUsage | None, bool]:
        occurred_at = self._clock()
        event_id = f"mge_{uuid4().hex}"
        request_id = getattr(event, "provider_request_id", None) or current_request_id
        if isinstance(event, AdapterStreamStarted):
            return (
                ResponseStartedEvent(
                    event_id=event_id,
                    event_type="response_started",
                    provider=route.provider,
                    model=route.model,
                    provider_request_id=request_id,
                    occurred_at=occurred_at,
                    payload=ResponseStartedPayload(),
                ),
                request_id,
                None,
                False,
            )
        if isinstance(event, AdapterTextDelta):
            return (
                TextDeltaEvent(
                    event_id=event_id,
                    event_type="text_delta",
                    provider=route.provider,
                    model=route.model,
                    provider_request_id=request_id,
                    occurred_at=occurred_at,
                    payload=TextDeltaPayload(delta=event.delta),
                ),
                request_id,
                None,
                False,
            )
        if isinstance(event, AdapterToolCallDelta):
            return (
                ToolCallDeltaEvent(
                    event_id=event_id,
                    event_type="tool_call_delta",
                    provider=route.provider,
                    model=route.model,
                    provider_request_id=request_id,
                    occurred_at=occurred_at,
                    payload=ToolCallDeltaPayload(
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        arguments_patch=event.arguments_patch,
                    ),
                ),
                request_id,
                None,
                False,
            )
        if isinstance(event, AdapterUsageEvent):
            normalized = normalize_usage(event.usage)
            return (
                self._usage_event(route, request_id, normalized),
                request_id,
                normalized,
                False,
            )
        output_ref = await self._write_output(context, request, event.output)
        return (
            ResponseCompletedEvent(
                event_id=event_id,
                event_type="response_completed",
                provider=route.provider,
                model=route.model,
                provider_request_id=request_id,
                occurred_at=occurred_at,
                payload=ResponseCompletedPayload(
                    finish_reason=event.finish_reason,
                    output_ref=output_ref,
                ),
            ),
            request_id,
            None,
            True,
        )

    async def _materialize(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> ModelInvocationInput:
        try:
            return await self._materializer.materialize(context, request)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise ProviderError(
                code="REQUEST_MATERIALIZATION_FAILED",
                message="The immutable model request could not be materialized.",
                retryable=False,
                submission_state="not_submitted",
            ) from error

    async def _write_output(
        self, context: TenantContext, request: ModelGatewayRequest, output: JsonValue
    ) -> ImmutableReference:
        try:
            return await self._output_writer.write(context, request, output)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise ProviderError(
                code="OUTPUT_PERSISTENCE_FAILED",
                message="The model output could not be persisted.",
                retryable=True,
                submission_state="submitted",
            ) from error

    def _usage_event(
        self, route: ModelRoute, provider_request_id: str | None, usage: ModelUsage
    ) -> UsageEvent:
        return UsageEvent(
            event_id=f"mge_{uuid4().hex}",
            event_type="usage",
            provider=route.provider,
            model=route.model,
            provider_request_id=provider_request_id,
            occurred_at=self._clock(),
            payload=UsagePayload(usage=usage),
        )

    def _error_event(
        self,
        route: ModelRoute,
        provider_request_id: str | None,
        error: ProviderError,
    ) -> ResponseErrorEvent:
        return ResponseErrorEvent(
            event_id=f"mge_{uuid4().hex}",
            event_type="response_error",
            provider=route.provider,
            model=route.model,
            provider_request_id=provider_request_id,
            occurred_at=self._clock(),
            payload=ResponseErrorPayload(
                code=error.code,
                message=error.safe_message,
                retryable=error.retryable,
                submission_state=error.submission_state,
            ),
        )


def _request_error_outcome(error: ProviderError) -> str:
    if error.code in {
        "BUDGET_GUARD_UNAVAILABLE",
        "COST_BUDGET_UNAVAILABLE",
        "GATEWAY_RATE_LIMITED",
        "RATE_LIMITER_UNAVAILABLE",
        "TOKEN_BUDGET_EXCEEDED",
    }:
        return "rejected"
    return "error"
