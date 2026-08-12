"""Event worker explicit tenant polling tests."""

from datetime import UTC, datetime
from typing import cast

import pytest

from apps.event_worker.runner import CompositeTenantOutboxDispatcher, dispatch_cycle
from packages.application.outbox import OutboxDispatcher, OutboxDispatchSummary
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.observability import PlatformMetrics


def context(tenant_id: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        subject_type=SubjectType.SERVICE,
        subject_id="33333333-3333-4333-8333-333333333333",
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
        request_id="req-event-worker",
        trace_id="trace-event-worker",
    )


class FakeContextSource:
    def __init__(self) -> None:
        self.limit: int | None = None

    async def list_service_contexts(self, *, limit: int) -> list[TenantContext]:
        self.limit = limit
        return [
            context("11111111-1111-4111-8111-111111111111"),
            context("22222222-2222-4222-8222-222222222222"),
        ]


class FakeDispatcher:
    def __init__(self) -> None:
        self.tenants: list[str] = []

    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary:
        self.tenants.append(context.tenant_id)
        return OutboxDispatchSummary(claimed=1, published=1)


@pytest.mark.asyncio
async def test_event_worker_dispatches_explicit_bounded_tenant_contexts() -> None:
    source = FakeContextSource()
    dispatcher = FakeDispatcher()

    dispatched = await dispatch_cycle(
        cast(OutboxDispatcher, dispatcher),
        source,
        PlatformMetrics(),
        tenant_limit=2,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )

    assert source.limit == 2
    assert dispatcher.tenants == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert dispatched == 2


@pytest.mark.asyncio
async def test_composite_dispatcher_aggregates_fixed_isolated_routes() -> None:
    first = FakeDispatcher()
    second = FakeDispatcher()
    composite = CompositeTenantOutboxDispatcher([first, second])
    tenant = context("11111111-1111-4111-8111-111111111111")

    summary = await composite.dispatch_tenant_once(
        tenant,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )

    assert first.tenants == second.tenants == [tenant.tenant_id]
    assert summary == OutboxDispatchSummary(claimed=2, published=2)
