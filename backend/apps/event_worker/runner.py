"""Independent bounded Outbox polling loop for the event-worker process."""

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
from packages.application.outbox import OutboxDispatchSummary
from packages.contracts.public import TenantContext
from packages.infrastructure.observability import PlatformMetrics


class TenantContextSource(Protocol):
    """Return a bounded, audited set of service contexts for one poll cycle."""

    async def list_service_contexts(self, *, limit: int) -> Sequence[TenantContext]: ...


class TenantOutboxDispatcher(Protocol):
    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary: ...


class CompositeTenantOutboxDispatcher:
    """Run a fixed, bounded set of isolated Outbox dispatchers per tenant."""

    def __init__(self, dispatchers: Sequence[TenantOutboxDispatcher]) -> None:
        if not dispatchers:
            raise ValueError("at least one tenant Outbox dispatcher is required")
        if len(dispatchers) > 16:
            raise ValueError("tenant Outbox dispatcher count cannot exceed 16")
        self._dispatchers = tuple(dispatchers)

    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary:
        total = OutboxDispatchSummary()
        for dispatcher in self._dispatchers:
            summary = await dispatcher.dispatch_tenant_once(context, now=now)
            total = OutboxDispatchSummary(
                claimed=total.claimed + summary.claimed,
                published=total.published + summary.published,
                retried=total.retried + summary.retried,
                dead=total.dead + summary.dead,
            )
        return total


async def dispatch_cycle(
    dispatcher: TenantOutboxDispatcher,
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
    dispatcher: TenantOutboxDispatcher,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    stop_event: asyncio.Event,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 1.0,
    recovery_policy: WorkerRecoveryPolicy | None = None,
) -> None:
    async def cycle() -> None:
        await dispatch_cycle(
            dispatcher,
            context_source,
            metrics,
            tenant_limit=tenant_limit,
        )

    await run_resilient_poll_loop(
        cycle,
        stop_event,
        metrics,
        process_name="event-worker",
        poll_interval_seconds=poll_interval_seconds,
        recovery_policy=recovery_policy,
    )


async def run_event_worker_process(
    dispatcher: TenantOutboxDispatcher,
    context_source: TenantContextSource,
    metrics: PlatformMetrics,
    *,
    tenant_limit: int = 100,
    poll_interval_seconds: float = 1.0,
    recovery_policy: WorkerRecoveryPolicy | None = None,
) -> None:
    """Run one explicitly composed Event Worker until SIGINT or SIGTERM."""

    async def worker(stop_event: asyncio.Event) -> None:
        await run_event_worker_loop(
            dispatcher,
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
        process_name=ProcessName.EVENT_WORKER.value,
    )
