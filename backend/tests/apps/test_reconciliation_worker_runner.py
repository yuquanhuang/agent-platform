"""Reconciliation worker loop aggregation tests."""

from datetime import UTC, datetime
from typing import cast

import pytest

from apps.reconciliation_worker.runner import reconciliation_cycle
from packages.application.reconciliation import RunReconciler, RunReconciliationSummary
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.observability import PlatformMetrics

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def context(value: str) -> TenantContext:
    return TenantContext(
        tenant_id=value,
        subject_type=SubjectType.SERVICE,
        subject_id="99999999-9999-4999-8999-999999999999",
        auth_time=NOW,
        request_id="req-cycle",
        trace_id="trace-cycle",
    )


class Source:
    async def list_service_contexts(self, *, limit: int) -> list[TenantContext]:
        assert limit == 2
        return [
            context("11111111-1111-4111-8111-111111111111"),
            context("22222222-2222-4222-8222-222222222222"),
        ]


class Reconciler:
    async def reconcile_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> RunReconciliationSummary:
        del context
        assert now == NOW
        return RunReconciliationSummary(
            examined=1,
            mappings_recorded=1,
            requests_requeued=1,
            cancellations_signalled=1,
            unresolved=1,
        )


@pytest.mark.asyncio
async def test_cycle_aggregates_each_explicit_tenant() -> None:
    summary = await reconciliation_cycle(
        cast(RunReconciler, Reconciler()),
        Source(),
        PlatformMetrics(),
        tenant_limit=2,
        now=NOW,
    )

    assert summary == RunReconciliationSummary(
        examined=2,
        mappings_recorded=2,
        requests_requeued=2,
        cancellations_signalled=2,
        unresolved=2,
    )
