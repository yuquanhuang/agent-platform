"""Fast admission-policy checks that do not require PostgreSQL."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    Money,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import ModelBinding, ModelRoute, ProviderError
from packages.infrastructure.model_gateway import (
    SqlAlchemyModelBudgetGuard,
    SqlAlchemyModelRateLimiter,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
HASH = "sha256:" + "a" * 64


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="22222222-2222-4222-8222-222222222222",
        auth_time=datetime(2026, 8, 7, tzinfo=UTC),
        request_id="req-policy",
        trace_id="trace-policy",
    )


def request() -> ModelGatewayRequest:
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
        capability_requirements=[],
        stream=False,
        timeout_seconds=30,
        idempotency_key="idem-123456",
        authorization_token="authorization-token-1234",
    )


def route(*, rate_limit_rpm: int | None = None) -> ModelRoute:
    return ModelRoute(
        provider="openai",
        model="test-model",
        base_url="https://provider.test/v1",
        secret_ref=f"secret://tenant/{TENANT_ID}/model/test",
        capabilities=frozenset(),
        timeout_seconds=30,
        rate_limit_rpm=rate_limit_rpm,
    )


def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(class_=AsyncSession)


@pytest.mark.asyncio
async def test_cost_budget_fails_closed_without_price_table() -> None:
    guard = SqlAlchemyModelBudgetGuard(session_factory())
    budgeted_request = request().model_copy(
        update={"cost_budget": Money(amount="1.00", currency="USD")}
    )

    with pytest.raises(ProviderError) as error:
        await guard.authorize(
            context(),
            budgeted_request,
            ModelBinding(binding_id="binding-1", routes=(route(),)),
        )

    assert error.value.code == "COST_BUDGET_UNAVAILABLE"
    assert error.value.submission_state == "not_submitted"


@pytest.mark.asyncio
async def test_unconfigured_token_budget_and_rpm_do_not_open_database_session() -> None:
    binding = ModelBinding(binding_id="binding-1", routes=(route(),))
    guard = SqlAlchemyModelBudgetGuard(session_factory())
    limiter = SqlAlchemyModelRateLimiter(session_factory())

    permit = await guard.authorize(context(), request(), binding)
    await limiter.acquire(context(), request(), binding, binding.routes[0])

    assert permit.reservation_id is None
    assert permit.reserved_tokens is None


@pytest.mark.asyncio
async def test_invalid_frozen_rpm_is_rejected_before_database_access() -> None:
    binding = ModelBinding(binding_id="binding-1", routes=(route(rate_limit_rpm=0),))
    limiter = SqlAlchemyModelRateLimiter(session_factory())

    with pytest.raises(ProviderError) as error:
        await limiter.acquire(context(), request(), binding, binding.routes[0])

    assert error.value.code == "INVALID_MODEL_BINDING"
