"""History-first RunEvent SSE orchestration with Redis-independent recovery."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from uuid import UUID

from packages.application.event_service.service import (
    RunEventQueryService,
    RunEventReadAccess,
)
from packages.application.metadata import RequestMetadata
from packages.contracts.generated.run_event import RunEvent
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    TenantContext,
)

TERMINAL_EVENT_TYPES = frozenset(
    {"run_succeeded", "run_failed", "run_cancelled", "run_timeout"}
)
SSE_HEARTBEAT_FRAME = b": heartbeat\n\n"


class RunEventNotificationUnavailable(RuntimeError):
    """Transient notification-channel failure; PostgreSQL polling remains valid."""


class RunEventNotificationSubscription(Protocol):
    async def wait(self, *, timeout_seconds: float) -> bool: ...

    async def aclose(self) -> None: ...


class RunEventNotificationSource(Protocol):
    async def subscribe(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunEventNotificationSubscription: ...


class RunEventStreamService:
    """Stream durable facts in sequence order and use notifications only to wake reads."""

    def __init__(
        self,
        query_service: RunEventQueryService,
        notification_source: RunEventNotificationSource | None = None,
        *,
        page_size: int = 200,
        heartbeat_seconds: float = 15.0,
        poll_interval_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError("SSE page_size must be between 1 and 200")
        if heartbeat_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("SSE timing values must be positive")
        self._query_service = query_service
        self._notification_source = notification_source
        self._page_size = page_size
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep

    async def open_stream(
        self,
        principal: AuthenticatedPrincipal,
        *,
        run_id: str,
        after: int,
        metadata: RequestMetadata,
    ) -> AsyncIterator[bytes]:
        access = await self._query_service.authorize(
            principal, run_id=run_id, metadata=metadata
        )
        return self._stream(access, after=after)

    async def _stream(
        self, access: RunEventReadAccess, *, after: int
    ) -> AsyncIterator[bytes]:
        cursor = after
        subscription = await self._try_subscribe(access)
        try:
            while True:
                try:
                    page = await self._query_service.read_page(
                        access, after=cursor, limit=self._page_size
                    )
                except PlatformError:
                    return
                for event in page.events:
                    if event.sequence_no <= cursor:
                        continue
                    yield _run_event_frame(event)
                    cursor = event.sequence_no
                    if event.event_type in TERMINAL_EVENT_TYPES:
                        return
                if page.has_more:
                    continue
                if page.is_terminal and cursor >= page.latest_sequence_no:
                    return
                notified = False
                if subscription is not None:
                    try:
                        notified = await subscription.wait(
                            timeout_seconds=self._heartbeat_seconds
                        )
                    except RunEventNotificationUnavailable:
                        await _close_subscription(subscription)
                        subscription = None
                if subscription is None:
                    await self._sleep(self._poll_interval_seconds)
                if not notified:
                    yield SSE_HEARTBEAT_FRAME
        finally:
            if subscription is not None:
                await _close_subscription(subscription)

    async def _try_subscribe(
        self, access: RunEventReadAccess
    ) -> RunEventNotificationSubscription | None:
        if self._notification_source is None:
            return None
        try:
            return await self._notification_source.subscribe(
                access.context, run_id=access.run_id
            )
        except RunEventNotificationUnavailable:
            return None


def _run_event_frame(event: RunEvent) -> bytes:
    payload = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"id: {event.sequence_no}\n" "event: run_event\n" f"data: {payload}\n\n"
    ).encode()


async def _close_subscription(
    subscription: RunEventNotificationSubscription,
) -> None:
    try:
        await subscription.aclose()
    except RunEventNotificationUnavailable:
        # Redis is only a wake-up channel; a close failure cannot invalidate
        # already authorized and persisted PostgreSQL events.
        return
