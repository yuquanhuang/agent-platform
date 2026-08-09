"""Outbox dispatcher transaction and failure convergence tests."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.application.outbox import (
    OutboxDispatcher,
    PermanentOutboxError,
    RetryableOutboxError,
    WorkflowStartResult,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.outbox import OutboxEvent, OutboxStatus

NOW = datetime(2026, 8, 6, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")


def tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="33333333-3333-4333-8333-333333333333",
        auth_time=NOW,
        request_id="req-outbox",
        trace_id="trace-outbox",
    )


def claimed_event(*, attempts: int = 1) -> OutboxEvent:
    return OutboxEvent(
        id=EVENT_ID,
        tenant_id=TENANT_ID,
        aggregate_type="probe",
        aggregate_id=UUID("44444444-4444-4444-8444-444444444444"),
        event_type="platform_probe_requested.v1",
        payload={},
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=attempts,
        next_attempt_at=NOW + timedelta(seconds=30),
        created_at=NOW,
    )


class FakeStore:
    def __init__(self, events: Sequence[OutboxEvent]) -> None:
        self.events = events
        self.actions: list[tuple[str, object]] = []
        self.transaction_active = False

    async def claim_ready(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
        lease_duration: timedelta,
    ) -> Sequence[OutboxEvent]:
        self.transaction_active = True
        self.actions.append(("claim", context.tenant_id))
        self.transaction_active = False
        return self.events[:limit]

    async def mark_published(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        self.actions.append(("published", event_id))

    async def mark_retry(
        self,
        context: TenantContext,
        event_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> None:
        self.actions.append(("retry", next_attempt_at))

    async def mark_dead(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        self.actions.append(("dead", event_id))


class FakeStarter:
    def __init__(self, store: FakeStore, error: Exception | None = None) -> None:
        self._store = store
        self._error = error

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        assert self._store.transaction_active is False
        if self._error is not None:
            raise self._error
        return WorkflowStartResult(
            workflow_id="probe/tenant/probe",
            run_id="temporal-run",
            already_exists=False,
        )


class FakeRecorder:
    def __init__(self, store: FakeStore, error: Exception | None = None) -> None:
        self._store = store
        self._error = error

    async def record(
        self,
        context: TenantContext,
        event: OutboxEvent,
        result: WorkflowStartResult,
        *,
        now: datetime,
    ) -> None:
        del context
        assert self._store.transaction_active is False
        self._store.actions.append(("recorded", event.id))
        assert result.workflow_id == "probe/tenant/probe"
        assert now == NOW
        if self._error is not None:
            raise self._error


@pytest.mark.asyncio
async def test_dispatcher_starts_temporal_after_claim_transaction_commits() -> None:
    store = FakeStore((claimed_event(),))
    dispatcher = OutboxDispatcher(
        store,
        FakeStarter(store),
        result_recorder=FakeRecorder(store),
    )

    summary = await dispatcher.dispatch_tenant_once(tenant_context(), now=NOW)

    assert summary.published == 1
    assert store.actions == [
        ("claim", str(TENANT_ID)),
        ("recorded", EVENT_ID),
        ("published", EVENT_ID),
    ]


@pytest.mark.asyncio
async def test_dispatcher_retries_when_start_result_persistence_fails() -> None:
    store = FakeStore((claimed_event(attempts=2),))
    dispatcher = OutboxDispatcher(
        store,
        FakeStarter(store),
        result_recorder=FakeRecorder(
            store, RetryableOutboxError("mapping unavailable")
        ),
    )

    summary = await dispatcher.dispatch_tenant_once(tenant_context(), now=NOW)

    assert summary.retried == 1
    assert store.actions[-1] == ("retry", NOW + timedelta(seconds=2))


@pytest.mark.asyncio
async def test_dispatcher_retries_with_deterministic_backoff() -> None:
    store = FakeStore((claimed_event(attempts=3),))
    dispatcher = OutboxDispatcher(
        store, FakeStarter(store, RetryableOutboxError("down"))
    )

    summary = await dispatcher.dispatch_tenant_once(tenant_context(), now=NOW)

    assert summary.retried == 1
    assert store.actions[-1] == ("retry", NOW + timedelta(seconds=4))


@pytest.mark.asyncio
async def test_dispatcher_marks_permanent_or_exhausted_failures_dead() -> None:
    permanent_store = FakeStore((claimed_event(),))
    permanent = OutboxDispatcher(
        permanent_store,
        FakeStarter(permanent_store, PermanentOutboxError("invalid")),
    )
    exhausted_store = FakeStore((claimed_event(attempts=10),))
    exhausted = OutboxDispatcher(
        exhausted_store,
        FakeStarter(exhausted_store, RetryableOutboxError("down")),
        max_attempts=10,
    )

    permanent_summary = await permanent.dispatch_tenant_once(tenant_context(), now=NOW)
    exhausted_summary = await exhausted.dispatch_tenant_once(tenant_context(), now=NOW)

    assert permanent_summary.dead == 1
    assert exhausted_summary.dead == 1
    assert permanent_store.actions[-1] == ("dead", EVENT_ID)
    assert exhausted_store.actions[-1] == ("dead", EVENT_ID)
