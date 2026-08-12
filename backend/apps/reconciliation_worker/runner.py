"""Bounded tenant-scoped platform reconciliation loop."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from apps.processes import ProcessName
from apps.worker_recovery import (
    WorkerRecoveryPolicy,
    run_polling_worker_process,
    run_resilient_poll_loop,
)
from packages.application.reconciliation import (
    ApprovalReconciliationSummary,
    PlatformReconciler,
    PlatformReconciliationSummary,
    RunReconciliationSummary,
    SandboxReconciliationSummary,
)
from packages.contracts.public import TenantContext
from packages.infrastructure.observability import PlatformMetrics


class TenantContextSource(Protocol):
    async def list_service_contexts(self, *, limit: int) -> Sequence[TenantContext]: ...


async def reconciliation_cycle(
    reconciler: PlatformReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int,
    now: datetime | None = None,
) -> PlatformReconciliationSummary:
    if tenant_limit < 1:
        raise ValueError("tenant_limit must be positive")
    contexts = await context_source.list_service_contexts(limit=tenant_limit)
    totals = PlatformReconciliationSummary()
    cycle_time = now or datetime.now(UTC)
    for context in contexts:
        summary = await reconciler.reconcile_tenant_once(context, now=cycle_time)
        metrics.observe_run_reconciliation(
            examined=summary.runs.examined,
            mappings_recorded=summary.runs.mappings_recorded,
            requests_requeued=summary.runs.requests_requeued,
            cancellations_signalled=summary.runs.cancellations_signalled,
            unresolved=summary.runs.unresolved,
        )
        metrics.observe_approval_reconciliation(
            expired=summary.approvals.expired,
            tickets_repaired=summary.approvals.tickets_repaired,
            signals_sent=summary.approvals.signals_sent,
            unresolved=summary.approvals.unresolved,
        )
        metrics.observe_sandbox_reconciliation(
            examined=summary.sandboxes.examined,
            destroyed=summary.sandboxes.destroyed,
            quarantined=summary.sandboxes.quarantined,
            cleanup_failed=summary.sandboxes.cleanup_failed,
            unresolved=summary.sandboxes.unresolved,
        )
        totals = _add_summary(totals, summary)
    return totals


def _add_summary(
    left: PlatformReconciliationSummary,
    right: PlatformReconciliationSummary,
) -> PlatformReconciliationSummary:
    return PlatformReconciliationSummary(
        runs=RunReconciliationSummary(
            examined=left.runs.examined + right.runs.examined,
            mappings_recorded=(
                left.runs.mappings_recorded + right.runs.mappings_recorded
            ),
            requests_requeued=(
                left.runs.requests_requeued + right.runs.requests_requeued
            ),
            cancellations_signalled=(
                left.runs.cancellations_signalled + right.runs.cancellations_signalled
            ),
            unresolved=left.runs.unresolved + right.runs.unresolved,
        ),
        approvals=ApprovalReconciliationSummary(
            expired=left.approvals.expired + right.approvals.expired,
            tickets_repaired=(
                left.approvals.tickets_repaired + right.approvals.tickets_repaired
            ),
            signals_sent=left.approvals.signals_sent + right.approvals.signals_sent,
            unresolved=left.approvals.unresolved + right.approvals.unresolved,
        ),
        sandboxes=SandboxReconciliationSummary(
            examined=left.sandboxes.examined + right.sandboxes.examined,
            destroyed=left.sandboxes.destroyed + right.sandboxes.destroyed,
            quarantined=left.sandboxes.quarantined + right.sandboxes.quarantined,
            cleanup_failed=(
                left.sandboxes.cleanup_failed + right.sandboxes.cleanup_failed
            ),
            unresolved=left.sandboxes.unresolved + right.sandboxes.unresolved,
        ),
    )


async def run_reconciliation_loop(
    reconciler: PlatformReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    stop_event: asyncio.Event,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 30.0,
    recovery_policy: WorkerRecoveryPolicy | None = None,
) -> None:
    async def cycle() -> None:
        await reconciliation_cycle(
            reconciler,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )

    await run_resilient_poll_loop(
        cycle,
        stop_event,
        metrics,
        process_name="reconciliation-worker",
        poll_interval_seconds=poll_interval_seconds,
        recovery_policy=recovery_policy,
    )


async def run_reconciliation_worker_process(
    reconciler: PlatformReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 30.0,
    recovery_policy: WorkerRecoveryPolicy | None = None,
) -> None:
    """Run one explicitly composed Reconciliation Worker until shutdown."""

    async def worker(stop_event: asyncio.Event) -> None:
        await run_reconciliation_loop(
            reconciler,
            context_source,
            metrics,
            stop_event,
            tenant_limit=tenant_limit,
            poll_interval_seconds=poll_interval_seconds,
            recovery_policy=recovery_policy,
        )

    await run_polling_worker_process(
        worker,
        metrics,
        process_name=ProcessName.RECONCILIATION_WORKER.value,
    )
