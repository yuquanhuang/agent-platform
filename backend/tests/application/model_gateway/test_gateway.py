"""Model Gateway kernel contract tests using only fake ports."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue, SecretStr

from packages.application.model_gateway import (
    BudgetPermit,
    ModelGatewayService,
    ProviderAdapterRegistry,
    normalize_usage,
)
from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ModelUsage,
    ResponseCompletedEvent,
    ResponseErrorEvent,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import (
    AdapterResponse,
    AdapterStreamCompleted,
    AdapterStreamEvent,
    AdapterStreamStarted,
    AdapterTextDelta,
    AdapterUsageEvent,
    ModelBinding,
    ModelInvocationInput,
    ModelRoute,
    ModelUsageRecord,
    ProviderConnectionTarget,
    ProviderError,
    ProviderUsage,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
HASH = "sha256:" + "a" * 64


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR_ID),
        membership_version=1,
        auth_time=datetime(2026, 8, 7, tzinfo=UTC),
        request_id="req-gateway",
        trace_id="trace-gateway",
    )


def request(*, stream: bool = False) -> ModelGatewayRequest:
    return ModelGatewayRequest(
        schema_version="1.0",
        tenant_id=str(TENANT_ID),
        user_id="user-1",
        agent_id="agent-1",
        snapshot_id="snapshot-1",
        run_id="run-1",
        model_binding_id="binding-1",
        prompt_ref=ImmutableReference(uri="prompt://p1", hash=HASH),
        tools_ref=ImmutableReference(uri="tools://t1", hash=HASH),
        capability_requirements=["stream"] if stream else [],
        parameters={},
        stream=stream,
        timeout_seconds=5,
        token_budget=None,
        cost_budget=None,
        idempotency_key="idem-123456",
        authorization_token="authorization-token-1234",
    )


def route(provider: str = "openai") -> ModelRoute:
    return ModelRoute(
        provider=provider,
        model="test-model",
        base_url="https://provider.test/v1",
        secret_ref=f"secret://tenant/{TENANT_ID}/model/test",
        capabilities=frozenset({"stream", "tools"}),
        timeout_seconds=5,
    )


class FakeBindingReader:
    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding:
        assert context.tenant_id == str(TENANT_ID)
        assert binding_id == "binding-1"
        return ModelBinding(binding_id=binding_id, routes=(route(),))


class FakeSecrets:
    async def resolve(self, context: TenantContext, secret_ref: str) -> SecretStr:
        assert context.tenant_id in secret_ref
        return SecretStr("provider-secret")


class FakeMaterializer:
    async def materialize(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> ModelInvocationInput:
        assert context.tenant_id == request.tenant_id
        return ModelInvocationInput(messages=({"role": "user", "content": "hello"},))


class FakeOutputWriter:
    def __init__(self) -> None:
        self.outputs: list[JsonValue] = []

    async def write(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        output: JsonValue,
    ) -> ImmutableReference:
        assert context.tenant_id == request.tenant_id
        self.outputs.append(output)
        return ImmutableReference(uri="object://gateway-output", hash=HASH)


class RecordingUsage:
    def __init__(self) -> None:
        self.records: list[ModelUsageRecord] = []

    async def record(self, context: TenantContext, usage: ModelUsageRecord) -> None:
        assert usage.tenant_id == TENANT_ID
        self.records.append(usage)


class RecordingBudgetGuard:
    def __init__(self, *, settle_error: ProviderError | None = None) -> None:
        self.permit = BudgetPermit(
            reservation_id=UUID("33333333-3333-4333-8333-333333333333"),
            reserved_tokens=10,
        )
        self.settle_error = settle_error
        self.authorized = 0
        self.settled: list[ModelUsage] = []
        self.released: list[BudgetPermit] = []

    async def authorize(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
    ) -> BudgetPermit:
        del context, request, binding
        self.authorized += 1
        return self.permit

    async def settle(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        permit: BudgetPermit,
        usage: ModelUsage,
    ) -> ProviderError | None:
        del context, request
        assert permit == self.permit
        self.settled.append(usage)
        return self.settle_error

    async def release(self, context: TenantContext, permit: BudgetPermit) -> None:
        del context
        self.released.append(permit)


class RejectingRateLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
        route: ModelRoute,
    ) -> None:
        del context, request, binding, route
        self.calls += 1
        raise ProviderError(
            code="GATEWAY_RATE_LIMITED",
            message="local RPM exceeded",
            retryable=True,
            submission_state="not_submitted",
        )


class FakeAdapter:
    def __init__(self, *, usage: ProviderUsage | None = None) -> None:
        self.usage = usage
        self.generate_calls = 0

    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse:
        del route, request
        assert invocation.messages[0]["content"] == "hello"
        assert credential.get_secret_value() == "provider-secret"
        self.generate_calls += 1
        return AdapterResponse(
            provider_request_id="provider-request-1",
            finish_reason="stop",
            output={"text": "hello"},
            usage=self.usage,
        )

    async def test_connection(
        self, target: ProviderConnectionTarget, credential: SecretStr
    ) -> None:
        del target
        assert credential.get_secret_value() == "provider-secret"

    async def stream(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AsyncIterator[AdapterStreamEvent]:
        del route, request, invocation, credential
        yield AdapterStreamStarted(provider_request_id="stream-request-1")
        yield AdapterTextDelta(delta="hello")
        yield AdapterUsageEvent(
            usage=ProviderUsage(input_tokens=2, output_tokens=1),
            provider_request_id="stream-request-1",
        )
        yield AdapterStreamCompleted(
            finish_reason="stop",
            output={"text": "hello"},
            provider_request_id="stream-request-1",
        )


class RetryableAdapter(FakeAdapter):
    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse:
        del route, request, invocation, credential
        raise ProviderError(
            code="RATE_LIMITED",
            message="provider rate limited",
            retryable=True,
            submission_state="not_submitted",
        )


class SecondaryBindingReader(FakeBindingReader):
    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding:
        del context
        return ModelBinding(
            binding_id=binding_id,
            routes=(route(), route("qwen")),
            max_fallbacks=1,
            fallback_error_codes=frozenset({"RATE_LIMITED"}),
        )


class UnsafeLocalFallbackBindingReader(FakeBindingReader):
    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding:
        del context
        return ModelBinding(
            binding_id=binding_id,
            routes=(route(), route("qwen")),
            max_fallbacks=1,
            fallback_error_codes=frozenset({"GATEWAY_RATE_LIMITED"}),
        )


@pytest.mark.asyncio
async def test_generate_normalizes_missing_usage_as_estimated() -> None:
    usage = RecordingUsage()
    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter()}),
        usage_recorder=usage,
    )

    response = await service.generate(context(), request())

    assert response.usage.input_tokens == 0
    assert response.usage.estimated is True
    assert response.fallback_count == 0
    assert len(usage.records) == 1


@pytest.mark.asyncio
async def test_generate_fallback_requires_not_submitted_retryable_error() -> None:
    usage = RecordingUsage()
    secondary = FakeAdapter(
        usage=ProviderUsage(
            input_tokens=3,
            output_tokens=2,
            reasoning_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    )
    service = ModelGatewayService(
        bindings=SecondaryBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry(
            {"openai": RetryableAdapter(), "qwen": secondary}
        ),
        usage_recorder=usage,
    )

    response = await service.generate(context(), request())

    assert response.provider == "qwen"
    assert response.fallback_count == 1
    assert response.usage.estimated is False


@pytest.mark.asyncio
async def test_generate_fallback_is_disabled_by_default() -> None:
    class DisabledFallbackBindingReader(FakeBindingReader):
        async def get_binding(
            self, context: TenantContext, binding_id: str
        ) -> ModelBinding:
            del context
            return ModelBinding(
                binding_id=binding_id,
                routes=(route(), route("qwen")),
            )

    secondary = FakeAdapter()
    service = ModelGatewayService(
        bindings=DisabledFallbackBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry(
            {"openai": RetryableAdapter(), "qwen": secondary}
        ),
        usage_recorder=RecordingUsage(),
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(context(), request())

    assert error.value.code == "RATE_LIMITED"
    assert secondary.generate_calls == 0


@pytest.mark.asyncio
async def test_local_rate_limit_never_falls_back_and_releases_budget() -> None:
    secondary = FakeAdapter()
    budget = RecordingBudgetGuard()
    limiter = RejectingRateLimiter()
    service = ModelGatewayService(
        bindings=UnsafeLocalFallbackBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter(), "qwen": secondary}),
        usage_recorder=RecordingUsage(),
        budget_guard=budget,
        rate_limiter=limiter,
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(context(), request())

    assert error.value.code == "GATEWAY_RATE_LIMITED"
    assert limiter.calls == 1
    assert secondary.generate_calls == 0
    assert budget.released == [budget.permit]


@pytest.mark.asyncio
async def test_default_budget_guard_fails_closed_for_budgeted_request() -> None:
    adapter = FakeAdapter()
    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": adapter}),
        usage_recorder=RecordingUsage(),
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(
            context(), request().model_copy(update={"token_budget": 10})
        )

    assert error.value.code == "BUDGET_GUARD_UNAVAILABLE"
    assert adapter.generate_calls == 0


@pytest.mark.asyncio
async def test_default_rate_limiter_fails_closed_for_frozen_rpm() -> None:
    class RateLimitedBindingReader(FakeBindingReader):
        async def get_binding(
            self, context: TenantContext, binding_id: str
        ) -> ModelBinding:
            del context
            limited_route = route()
            return ModelBinding(
                binding_id=binding_id,
                routes=(
                    ModelRoute(
                        provider=limited_route.provider,
                        model=limited_route.model,
                        base_url=limited_route.base_url,
                        secret_ref=limited_route.secret_ref,
                        capabilities=limited_route.capabilities,
                        timeout_seconds=limited_route.timeout_seconds,
                        rate_limit_rpm=10,
                    ),
                ),
            )

    adapter = FakeAdapter()
    service = ModelGatewayService(
        bindings=RateLimitedBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": adapter}),
        usage_recorder=RecordingUsage(),
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(context(), request())

    assert error.value.code == "RATE_LIMITER_UNAVAILABLE"
    assert adapter.generate_calls == 0


@pytest.mark.asyncio
async def test_generate_records_usage_before_budget_overrun_is_reported() -> None:
    usage = RecordingUsage()
    budget = RecordingBudgetGuard(
        settle_error=ProviderError(
            code="TOKEN_BUDGET_EXCEEDED",
            message="actual usage exceeded reservation",
            retryable=False,
            submission_state="submitted",
        )
    )
    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry(
            {
                "openai": FakeAdapter(
                    usage=ProviderUsage(input_tokens=7, output_tokens=5)
                )
            }
        ),
        usage_recorder=usage,
        budget_guard=budget,
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(context(), request())

    assert error.value.code == "TOKEN_BUDGET_EXCEEDED"
    assert len(usage.records) == 1
    assert budget.settled[0].input_tokens == 7
    assert budget.released == []


@pytest.mark.asyncio
async def test_generate_unknown_submission_state_never_falls_back() -> None:
    class UnknownStateAdapter(FakeAdapter):
        async def generate(
            self,
            route: ModelRoute,
            request: ModelGatewayRequest,
            invocation: ModelInvocationInput,
            credential: SecretStr,
        ) -> AdapterResponse:
            del route, request, invocation, credential
            raise ProviderError(
                code="UNKNOWN_SUBMISSION_STATE",
                message="submission state cannot be confirmed",
                retryable=True,
                submission_state="unknown",
            )

    service = ModelGatewayService(
        bindings=SecondaryBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry(
            {"openai": UnknownStateAdapter(), "qwen": FakeAdapter()}
        ),
        usage_recorder=RecordingUsage(),
    )

    with pytest.raises(ProviderError) as error:
        await service.generate(context(), request())
    assert error.value.code == "UNKNOWN_SUBMISSION_STATE"


@pytest.mark.asyncio
async def test_stream_emits_started_delta_usage_and_completed_events() -> None:
    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter()}),
        usage_recorder=RecordingUsage(),
    )

    events = [event async for event in service.stream(context(), request(stream=True))]

    assert [event.event_type for event in events] == [
        "response_started",
        "text_delta",
        "usage",
        "response_completed",
    ]
    completed = events[-1]
    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.payload.finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_returns_safe_error_after_provider_failure() -> None:
    class FailingAdapter(FakeAdapter):
        async def stream(
            self,
            route: ModelRoute,
            request: ModelGatewayRequest,
            invocation: ModelInvocationInput,
            credential: SecretStr,
        ) -> AsyncIterator[AdapterStreamEvent]:
            del route, request, invocation, credential
            yield AdapterStreamStarted(provider_request_id="submitted-1")
            raise ProviderError(
                code="PROVIDER_UNAVAILABLE",
                message="provider raw body must not leak",
                retryable=True,
                submission_state="submitted",
            )

    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FailingAdapter()}),
        usage_recorder=RecordingUsage(),
    )

    events = [event async for event in service.stream(context(), request(stream=True))]

    assert isinstance(events[-1], ResponseErrorEvent)
    assert events[-1].payload.code == "PROVIDER_UNAVAILABLE"
    assert events[-1].payload.message == "provider raw body must not leak"
    assert events[-1].payload.submission_state == "submitted"


@pytest.mark.asyncio
async def test_stream_replaces_completed_event_with_budget_error() -> None:
    budget = RecordingBudgetGuard(
        settle_error=ProviderError(
            code="TOKEN_BUDGET_EXCEEDED",
            message="actual usage exceeded reservation",
            retryable=False,
            submission_state="submitted",
        )
    )
    service = ModelGatewayService(
        bindings=FakeBindingReader(),
        materializer=FakeMaterializer(),
        output_writer=FakeOutputWriter(),
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter()}),
        usage_recorder=RecordingUsage(),
        budget_guard=budget,
    )

    events = [event async for event in service.stream(context(), request(stream=True))]

    assert [event.event_type for event in events] == [
        "response_started",
        "text_delta",
        "usage",
        "response_error",
    ]
    assert isinstance(events[-1], ResponseErrorEvent)
    assert events[-1].payload.code == "TOKEN_BUDGET_EXCEEDED"
    assert events[-1].payload.submission_state == "submitted"


def test_normalize_usage_rejects_negative_provider_values() -> None:
    with pytest.raises(ProviderError) as error:
        normalize_usage(ProviderUsage(input_tokens=-1))
    assert error.value.code == "PROVIDER_PROTOCOL_ERROR"


def test_model_binding_rejects_more_than_two_fallback_levels() -> None:
    with pytest.raises(ValueError, match="at most two fallbacks"):
        ModelBinding(
            binding_id="binding-1",
            routes=(route(), route("qwen"), route("deepseek"), route("openai")),
        )


def test_model_binding_enforces_configured_fallback_count() -> None:
    binding = ModelBinding(
        binding_id="binding-1",
        routes=(route(), route("qwen"), route("deepseek")),
        max_fallbacks=1,
        fallback_error_codes=frozenset({"RATE_LIMITED"}),
    )
    error = ProviderError(
        code="RATE_LIMITED",
        message="provider rate limited",
        retryable=True,
        submission_state="not_submitted",
    )

    assert binding.allows_fallback(error, 0) is True
    assert binding.allows_fallback(error, 1) is False
