"""Reconciliation worker loop aggregation tests."""

from datetime import UTC, datetime
from typing import cast

import pytest

from apps.reconciliation_worker.runner import reconciliation_cycle, run_admission_cycle
from packages.application.reconciliation import (
    ApprovalReconciliationSummary,
    PlatformReconciler,
    PlatformReconciliationSummary,
    RunReconciliationSummary,
    SandboxReconciliationSummary,
)
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
    ) -> PlatformReconciliationSummary:
        del context
        assert now == NOW
        return PlatformReconciliationSummary(
            runs=RunReconciliationSummary(
                examined=1,
                mappings_recorded=1,
                requests_requeued=1,
                cancellations_signalled=1,
                unresolved=1,
            ),
            approvals=ApprovalReconciliationSummary(
                expired=1,
                tickets_repaired=1,
                signals_sent=1,
                unresolved=1,
            ),
            sandboxes=SandboxReconciliationSummary(
                examined=2,
                destroyed=1,
                quarantined=1,
                cleanup_failed=1,
                unresolved=1,
            ),
        )

    async def process_run_admission_queue(
        self, context: TenantContext, *, now: datetime
    ) -> RunReconciliationSummary:
        del context
        assert now == NOW
        return RunReconciliationSummary(queue_admitted=2, queue_timed_out=1)


@pytest.mark.asyncio
async def test_cycle_aggregates_each_explicit_tenant() -> None:
    summary = await reconciliation_cycle(
        cast(PlatformReconciler, Reconciler()),
        Source(),
        PlatformMetrics(),
        tenant_limit=2,
        now=NOW,
    )

    assert summary == PlatformReconciliationSummary(
        runs=RunReconciliationSummary(
            examined=2,
            mappings_recorded=2,
            requests_requeued=2,
            cancellations_signalled=2,
            unresolved=2,
        ),
        approvals=ApprovalReconciliationSummary(
            expired=2,
            tickets_repaired=2,
            signals_sent=2,
            unresolved=2,
        ),
        sandboxes=SandboxReconciliationSummary(
            examined=4,
            destroyed=2,
            quarantined=2,
            cleanup_failed=2,
            unresolved=2,
        ),
    )


@pytest.mark.asyncio
async def test_queue_cycle_does_not_run_other_reconcilers() -> None:
    summary = await run_admission_cycle(
        cast(PlatformReconciler, Reconciler()),
        Source(),
        PlatformMetrics(),
        tenant_limit=2,
        now=NOW,
    )

    assert summary == RunReconciliationSummary(
        queue_admitted=4,
        queue_timed_out=2,
    )
