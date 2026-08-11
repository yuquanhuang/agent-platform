"""Durable RunEvent Outbox delivery into an ephemeral notification channel."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from packages.application.outbox import (
    OutboxDispatchSummary,
    OutboxStore,
    PermanentOutboxError,
    RetryableOutboxError,
)
from packages.contracts.public import TenantContext
from packages.domain.public import OutboxEvent, retry_delay

RUN_EVENTS_APPENDED_EVENT = "run.events_appended.v1"


@dataclass(frozen=True, slots=True)
class RunEventNotification:
    notification_id: UUID
    tenant_id: UUID
    run_id: UUID
    first_sequence_no: int
    last_sequence_no: int
    event_count: int


class RunEventNotificationPublisher(Protocol):
    async def publish(self, notification: RunEventNotification) -> None: ...


class RunEventNotificationDispatcher:
    """Claim only RunEvent notifications and acknowledge after Redis publish."""

    def __init__(
        self,
        store: OutboxStore,
        publisher: RunEventNotificationPublisher,
        *,
        batch_size: int = 50,
        max_attempts: int = 10,
        lease_duration: timedelta = timedelta(seconds=30),
        publish_timeout: timedelta = timedelta(seconds=5),
    ) -> None:
        if batch_size < 1 or max_attempts < 1:
            raise ValueError("batch_size and max_attempts must be positive")
        if lease_duration <= timedelta(0) or publish_timeout <= timedelta(0):
            raise ValueError("notification dispatcher timeouts must be positive")
        self._store = store
        self._publisher = publisher
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_duration = lease_duration
        self._publish_timeout = publish_timeout

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
                notification = _notification(event)
                await asyncio.wait_for(
                    self._publisher.publish(notification),
                    timeout=self._publish_timeout.total_seconds(),
                )
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


def _notification(event: OutboxEvent) -> RunEventNotification:
    if event.event_type != RUN_EVENTS_APPENDED_EVENT:
        raise PermanentOutboxError(
            f"unsupported notification event_type: {event.event_type}"
        )
    if event.aggregate_type != "run_event" or event.payload_schema_version != 1:
        raise PermanentOutboxError("RunEvent notification envelope is invalid")
    try:
        tenant_id = UUID(_string(event.payload["tenant_id"]))
        run_id = UUID(_string(event.payload["run_id"]))
        first_sequence_no = _positive_int(event.payload["first_sequence_no"])
        last_sequence_no = _positive_int(event.payload["last_sequence_no"])
        event_count = _positive_int(event.payload["event_count"])
    except (KeyError, TypeError, ValueError) as error:
        raise PermanentOutboxError(
            "RunEvent notification payload is invalid"
        ) from error
    if (
        tenant_id != event.tenant_id
        or run_id != event.aggregate_id
        or last_sequence_no < first_sequence_no
        or event_count != last_sequence_no - first_sequence_no + 1
    ):
        raise PermanentOutboxError("RunEvent notification facts are inconsistent")
    return RunEventNotification(
        notification_id=event.id,
        tenant_id=tenant_id,
        run_id=run_id,
        first_sequence_no=first_sequence_no,
        last_sequence_no=last_sequence_no,
        event_count=event_count,
    )


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TypeError
    return value
