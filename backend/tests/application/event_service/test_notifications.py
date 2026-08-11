"""RunEvent Outbox notification dispatcher tests."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from packages.application.event_service import (
    RUN_EVENTS_APPENDED_EVENT,
    RunEventNotification,
    RunEventNotificationDispatcher,
    RunEventNotificationPublisher,
)
from packages.application.outbox import OutboxStore, RetryableOutboxError
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import OutboxEvent, OutboxStatus

NOW = datetime(2026, 8, 9, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="44444444-4444-4444-8444-444444444444",
        auth_time=NOW,
        request_id="req-run-event-notification",
        trace_id="trace-run-event-notification",
    )


def event(
    *,
    attempts: int = 1,
    event_type: str = RUN_EVENTS_APPENDED_EVENT,
    aggregate_type: str = "run_event",
    payload: dict[str, object] | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        id=EVENT_ID,
        tenant_id=TENANT_ID,
        aggregate_type=aggregate_type,
        aggregate_id=RUN_ID,
        event_type=event_type,
        payload=payload
        or {
            "tenant_id": str(TENANT_ID),
            "run_id": str(RUN_ID),
            "first_sequence_no": 7,
            "last_sequence_no": 9,
            "event_count": 3,
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=attempts,
        next_attempt_at=NOW,
        created_at=NOW,
    )


class FakeStore:
    def __init__(self, events: Sequence[OutboxEvent]) -> None:
        self.events = events
        self.actions: list[tuple[str, object]] = []

    async def claim_ready(
        self,
        _context: TenantContext,
        *,
        now: datetime,
        limit: int,
        lease_duration: timedelta,
    ) -> Sequence[OutboxEvent]:
        del now, lease_duration
        return self.events[:limit]

    async def mark_published(
        self, _context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        del now
        self.actions.append(("published", event_id))

    async def mark_retry(
        self,
        _context: TenantContext,
        event_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> None:
        del event_id
        self.actions.append(("retry", next_attempt_at))

    async def mark_dead(
        self, _context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        del now
        self.actions.append(("dead", event_id))


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.notifications: list[RunEventNotification] = []

    async def publish(self, notification: RunEventNotification) -> None:
        if self.error is not None:
            raise self.error
        self.notifications.append(notification)


@pytest.mark.asyncio
async def test_dispatcher_publishes_valid_wakeup_then_acknowledges_outbox() -> None:
    store = FakeStore((event(),))
    publisher = FakePublisher()
    dispatcher = RunEventNotificationDispatcher(
        cast(OutboxStore, store), cast(RunEventNotificationPublisher, publisher)
    )

    summary = await dispatcher.dispatch_tenant_once(context(), now=NOW)

    assert summary.published == 1
    assert publisher.notifications == [
        RunEventNotification(
            notification_id=EVENT_ID,
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            first_sequence_no=7,
            last_sequence_no=9,
            event_count=3,
        )
    ]
    assert store.actions == [("published", EVENT_ID)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_event",
    [
        event(event_type="agent.run_requested.v1"),
        event(aggregate_type="run"),
        event(
            payload={
                "tenant_id": str(TENANT_ID),
                "run_id": str(RUN_ID),
                "first_sequence_no": 7,
                "last_sequence_no": 9,
                "event_count": 2,
            }
        ),
    ],
)
async def test_dispatcher_dead_letters_invalid_envelopes(
    bad_event: OutboxEvent,
) -> None:
    store = FakeStore((bad_event,))
    dispatcher = RunEventNotificationDispatcher(
        cast(OutboxStore, store),
        cast(RunEventNotificationPublisher, FakePublisher()),
    )

    summary = await dispatcher.dispatch_tenant_once(context(), now=NOW)

    assert summary.dead == 1
    assert store.actions == [("dead", EVENT_ID)]


@pytest.mark.asyncio
async def test_dispatcher_retries_transient_publish_failure_with_backoff() -> None:
    store = FakeStore((event(attempts=3),))
    dispatcher = RunEventNotificationDispatcher(
        cast(OutboxStore, store),
        cast(
            RunEventNotificationPublisher,
            FakePublisher(RetryableOutboxError("redis unavailable")),
        ),
    )

    summary = await dispatcher.dispatch_tenant_once(context(), now=NOW)

    assert summary.retried == 1
    assert store.actions == [("retry", NOW + timedelta(seconds=4))]


@pytest.mark.asyncio
async def test_dispatcher_dead_letters_exhausted_publish_failure() -> None:
    store = FakeStore((event(attempts=4),))
    dispatcher = RunEventNotificationDispatcher(
        cast(OutboxStore, store),
        cast(
            RunEventNotificationPublisher,
            FakePublisher(RetryableOutboxError("redis unavailable")),
        ),
        max_attempts=4,
    )

    summary = await dispatcher.dispatch_tenant_once(context(), now=NOW)

    assert summary.dead == 1
    assert store.actions == [("dead", EVENT_ID)]
