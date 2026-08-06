"""Independent bounded Outbox polling loop for the event-worker process."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from packages.application.outbox import OutboxDispatcher
from packages.contracts.public import TenantContext
from packages.infrastructure.observability import PlatformMetrics


class TenantContextSource(Protocol):
    """Return a bounded, audited set of service contexts for one poll cycle."""

    async def list_service_contexts(self, *, limit: int) -> Sequence[TenantContext]: ...


async def dispatch_cycle(
    dispatcher: OutboxDispatcher,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int,
    now: datetime | None = None,
) -> int:
    """Dispatch each tenant explicitly; never issue an unscoped Outbox query."""

    if tenant_limit < 1:
        raise ValueError("tenant_limit must be positive")
    contexts = await context_source.list_service_contexts(limit=tenant_limit)
    dispatched = 0
    cycle_time = now or datetime.now(UTC)
    for context in contexts:
        summary = await dispatcher.dispatch_tenant_once(context, now=cycle_time)
        metrics.observe_outbox(
            claimed=summary.claimed,
            published=summary.published,
            retried=summary.retried,
            dead=summary.dead,
        )
        dispatched += summary.claimed
    return dispatched


async def run_event_worker_loop(
    dispatcher: OutboxDispatcher,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    stop_event: asyncio.Event,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 1.0,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    while not stop_event.is_set():
        await dispatch_cycle(
            dispatcher,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            continue
