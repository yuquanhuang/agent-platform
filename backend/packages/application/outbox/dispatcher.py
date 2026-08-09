"""Database-transaction-outside workflow dispatch orchestration."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from packages.contracts.public import TenantContext
from packages.domain.outbox import OutboxEvent, retry_delay


@dataclass(frozen=True, slots=True)
class WorkflowStartResult:
    workflow_id: str
    run_id: str | None
    already_exists: bool


class PermanentOutboxError(ValueError):
    """Payload or routing error that retry cannot repair."""


class RetryableOutboxError(RuntimeError):
    """Dependency failure that can be retried with bounded backoff."""


class WorkflowStarter(Protocol):
    async def start(self, event: OutboxEvent) -> WorkflowStartResult: ...


class WorkflowStartResultRecorder(Protocol):
    """Persist a successful external start before the Outbox is acknowledged."""

    async def record(
        self,
        context: TenantContext,
        event: OutboxEvent,
        result: WorkflowStartResult,
        *,
        now: datetime,
    ) -> None: ...


class OutboxStore(Protocol):
    """Each method owns a short database transaction and returns after commit."""

    async def claim_ready(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
        lease_duration: timedelta,
    ) -> Sequence[OutboxEvent]: ...

    async def mark_published(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None: ...

    async def mark_retry(
        self,
        context: TenantContext,
        event_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> None: ...

    async def mark_dead(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OutboxDispatchSummary:
    claimed: int = 0
    published: int = 0
    retried: int = 0
    dead: int = 0


class OutboxDispatcher:
    """Claim in one transaction, call Temporal, then persist the outcome."""

    def __init__(
        self,
        store: OutboxStore,
        starter: WorkflowStarter,
        *,
        result_recorder: WorkflowStartResultRecorder | None = None,
        batch_size: int = 50,
        max_attempts: int = 10,
        lease_duration: timedelta = timedelta(seconds=30),
        start_timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        if batch_size < 1 or max_attempts < 1:
            raise ValueError("batch_size and max_attempts must be positive")
        if lease_duration <= timedelta(0) or start_timeout <= timedelta(0):
            raise ValueError("dispatcher timeouts must be positive")
        self._store = store
        self._starter = starter
        self._result_recorder = result_recorder
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_duration = lease_duration
        self._start_timeout = start_timeout

    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary:
        claimed = await self._store.claim_ready(
            context,
            now=now,
            limit=self._batch_size,
            lease_duration=self._lease_duration,
        )
        published = retried = dead = 0
        for event in claimed:
            try:
                result = await asyncio.wait_for(
                    self._starter.start(event),
                    timeout=self._start_timeout.total_seconds(),
                )
                if self._result_recorder is not None:
                    await self._result_recorder.record(context, event, result, now=now)
            except PermanentOutboxError:
                await self._store.mark_dead(context, event.id, now=now)
                dead += 1
            except asyncio.CancelledError:
                raise
            except (RetryableOutboxError, TimeoutError):
                if event.attempts >= self._max_attempts:
                    await self._store.mark_dead(context, event.id, now=now)
                    dead += 1
                else:
                    await self._store.mark_retry(
                        context,
                        event.id,
                        next_attempt_at=now + retry_delay(event.attempts),
                    )
                    retried += 1
            else:
                await self._store.mark_published(context, event.id, now=now)
                published += 1
        return OutboxDispatchSummary(
            claimed=len(claimed),
            published=published,
            retried=retried,
            dead=dead,
        )
