"""Bounded tenant-scoped Run reconciliation loop."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from packages.application.reconciliation import RunReconciler, RunReconciliationSummary
from packages.contracts.public import TenantContext
from packages.infrastructure.observability import PlatformMetrics


class TenantContextSource(Protocol):
    async def list_service_contexts(self, *, limit: int) -> Sequence[TenantContext]: ...


async def reconciliation_cycle(
    reconciler: RunReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int,
    now: datetime | None = None,
) -> RunReconciliationSummary:
    if tenant_limit < 1:
        raise ValueError("tenant_limit must be positive")
    contexts = await context_source.list_service_contexts(limit=tenant_limit)
    totals = RunReconciliationSummary()
    cycle_time = now or datetime.now(UTC)
    for context in contexts:
        summary = await reconciler.reconcile_tenant_once(context, now=cycle_time)
        metrics.observe_run_reconciliation(
            examined=summary.examined,
            mappings_recorded=summary.mappings_recorded,
            requests_requeued=summary.requests_requeued,
            cancellations_signalled=summary.cancellations_signalled,
            unresolved=summary.unresolved,
        )
        totals = RunReconciliationSummary(
            examined=totals.examined + summary.examined,
            mappings_recorded=(totals.mappings_recorded + summary.mappings_recorded),
            requests_requeued=totals.requests_requeued + summary.requests_requeued,
            cancellations_signalled=(
                totals.cancellations_signalled + summary.cancellations_signalled
            ),
            unresolved=totals.unresolved + summary.unresolved,
        )
    return totals


async def run_reconciliation_loop(
    reconciler: RunReconciler,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    stop_event: asyncio.Event,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 30.0,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    while not stop_event.is_set():
        await reconciliation_cycle(
            reconciler,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            continue
