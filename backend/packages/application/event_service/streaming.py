"""History-first RunEvent SSE orchestration with Redis-independent recovery."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
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


class RunEventStreamMetrics(Protocol):
    def sse_opened(self) -> None: ...

    def sse_closed(self, *, outcome: str) -> None: ...

    def observe_sse_connection_outcome(self, *, outcome: str) -> None: ...

    def observe_sse_frame(
        self, *, frame_type: str, visibility_delay_seconds: float | None = None
    ) -> None: ...


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
        metrics: RunEventStreamMetrics | None = None,
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
        self._metrics = metrics

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
        subscription: RunEventNotificationSubscription | None = None
        close_outcome = "terminal"
        fallback_observed = False
        if self._metrics is not None:
            self._metrics.sse_opened()
        try:
            subscription = await self._try_subscribe(access)
            if subscription is None and self._metrics is not None:
                self._metrics.observe_sse_connection_outcome(
                    outcome="notification_fallback"
                )
                fallback_observed = True
            while True:
                try:
                    page = await self._query_service.read_page(
                        access, after=cursor, limit=self._page_size
                    )
                except PlatformError:
                    close_outcome = "query_error"
                    return
                for event in page.events:
                    if event.sequence_no <= cursor:
                        continue
                    yield _run_event_frame(event)
                    if self._metrics is not None:
                        self._metrics.observe_sse_frame(
                            frame_type="run_event",
                            visibility_delay_seconds=_visibility_delay(
                                event.recorded_at
                            ),
                        )
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
                    if self._metrics is not None and not fallback_observed:
                        self._metrics.observe_sse_connection_outcome(
                            outcome="notification_fallback"
                        )
                        fallback_observed = True
                    await self._sleep(self._poll_interval_seconds)
                if not notified:
                    yield SSE_HEARTBEAT_FRAME
                    if self._metrics is not None:
                        self._metrics.observe_sse_frame(frame_type="heartbeat")
        finally:
            if subscription is not None:
                await _close_subscription(subscription)
            if self._metrics is not None:
                self._metrics.sse_closed(outcome=close_outcome)

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


def _visibility_delay(recorded_at: datetime) -> float:
    now = datetime.now(UTC)
    if recorded_at.tzinfo is None:
        return 0.0
    return max(0.0, (now - recorded_at).total_seconds())


async def _close_subscription(
    subscription: RunEventNotificationSubscription,
) -> None:
    try:
        await subscription.aclose()
    except RunEventNotificationUnavailable:
        # Redis is only a wake-up channel; a close failure cannot invalidate
        # already authorized and persisted PostgreSQL events.
        return
