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
            queue_admitted=summary.runs.queue_admitted,
            queue_timed_out=summary.runs.queue_timed_out,
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


async def run_admission_cycle(
    reconciler: PlatformReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int,
    now: datetime | None = None,
) -> RunReconciliationSummary:
    """Process only durable Run queue deadlines and capacity admission."""

    if tenant_limit < 1:
        raise ValueError("tenant_limit must be positive")
    contexts = await context_source.list_service_contexts(limit=tenant_limit)
    cycle_time = now or datetime.now(UTC)
    process_domains = getattr(reconciler, "process_capacity_admission_domains", None)
    if process_domains is not None and contexts:
        summary = await process_domains(contexts[0], now=cycle_time)
        metrics.observe_run_reconciliation(
            examined=0,
            mappings_recorded=0,
            requests_requeued=0,
            cancellations_signalled=0,
            unresolved=0,
            queue_admitted=summary.queue_admitted,
            queue_timed_out=summary.queue_timed_out,
        )
        metrics.observe_capacity_leases(
            released=summary.capacity_leases_released,
            renewed=summary.capacity_leases_renewed,
        )
        return summary
    total = RunReconciliationSummary()
    for context in contexts:
        summary = await reconciler.process_run_admission_queue(context, now=cycle_time)
        metrics.observe_run_reconciliation(
            examined=0,
            mappings_recorded=0,
            requests_requeued=0,
            cancellations_signalled=0,
            unresolved=0,
            queue_admitted=summary.queue_admitted,
            queue_timed_out=summary.queue_timed_out,
        )
        metrics.observe_capacity_leases(
            released=summary.capacity_leases_released,
            renewed=summary.capacity_leases_renewed,
        )
        total = RunReconciliationSummary(
            queue_admitted=total.queue_admitted + summary.queue_admitted,
            queue_timed_out=total.queue_timed_out + summary.queue_timed_out,
            capacity_leases_released=(
                total.capacity_leases_released + summary.capacity_leases_released
            ),
            capacity_leases_renewed=(
                total.capacity_leases_renewed + summary.capacity_leases_renewed
            ),
        )
    return total


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
            queue_admitted=left.runs.queue_admitted + right.runs.queue_admitted,
            queue_timed_out=left.runs.queue_timed_out + right.runs.queue_timed_out,
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
    run_queue_poll_interval_seconds: float = 1.0,
    recovery_policy: WorkerRecoveryPolicy | None = None,
) -> None:
    async def full_cycle() -> None:
        await reconciliation_cycle(
            reconciler,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )

    async def queue_cycle() -> None:
        await run_admission_cycle(
            reconciler,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )

    async with asyncio.TaskGroup() as group:
        group.create_task(
            run_resilient_poll_loop(
                full_cycle,
                stop_event,
                metrics,
                process_name="reconciliation-worker",
                poll_interval_seconds=poll_interval_seconds,
                recovery_policy=recovery_policy,
            )
        )
        group.create_task(
            run_resilient_poll_loop(
                queue_cycle,
                stop_event,
                metrics,
                process_name="run-admission-scheduler",
                poll_interval_seconds=run_queue_poll_interval_seconds,
                recovery_policy=recovery_policy,
            )
        )


async def run_reconciliation_worker_process(
    reconciler: PlatformReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 30.0,
    run_queue_poll_interval_seconds: float = 1.0,
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
            run_queue_poll_interval_seconds=run_queue_poll_interval_seconds,
            recovery_policy=recovery_policy,
        )

    await run_polling_worker_process(
        worker,
        metrics,
        process_name=ProcessName.RECONCILIATION_WORKER.value,
    )
