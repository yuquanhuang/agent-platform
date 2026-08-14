"""History-first SSE wake-up, fallback polling and terminal-close tests."""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from prometheus_client import generate_latest
from pydantic import JsonValue

from packages.application.event_service import (
    RunEventNotificationSource,
    RunEventNotificationSubscription,
    RunEventNotificationUnavailable,
    RunEventPageRecord,
    RunEventQueryService,
    RunEventQueryStore,
    RunEventRecord,
    RunEventStreamService,
)
from packages.application.public import RequestMetadata
from packages.contracts.public import AuthenticatedPrincipal, SubjectType, TenantContext
from packages.domain.public import TenantAccess
from packages.infrastructure.observability import PlatformMetrics

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 9, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-sse", trace_id="trace-sse")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="sse-user",
        display_name="SSE User",
        platform_roles=frozenset(),
        auth_time=NOW,
    )


class Resolver:
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR_ID),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset({"run:read"}),
        )


class MutableEventStore:
    def __init__(self, events: list[RunEventRecord]) -> None:
        self.events = events
        self.reads: list[int] = []

    async def list_events(self, context: TenantContext, **kwargs: object):
        del context
        after = cast(int, kwargs["after"])
        limit = cast(int, kwargs["limit"])
        self.reads.append(after)
        remaining = [event for event in self.events if event.sequence_no > after]
        page_events = remaining[:limit]
        latest = self.events[-1].sequence_no if self.events else 0
        terminal = bool(self.events) and self.events[-1].event_type in {
            "run_succeeded",
            "run_failed",
            "run_cancelled",
            "run_timeout",
        }
        return RunEventPageRecord(
            events=tuple(page_events),
            latest_sequence_no=latest,
            has_more=len(remaining) > limit,
            is_terminal=terminal
            and (not page_events or page_events[-1].sequence_no == latest),
        )


class Subscription:
    def __init__(self, on_wait: Callable[[], None]) -> None:
        self._on_wait = on_wait
        self.closed = False
        self.waits = 0

    async def wait(self, *, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        self.waits += 1
        self._on_wait()
        return True

    async def aclose(self) -> None:
        self.closed = True


class Source:
    def __init__(self, subscription: Subscription) -> None:
        self.subscription = subscription
        self.contexts: list[str] = []

    async def subscribe(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunEventNotificationSubscription:
        assert run_id == RUN_ID
        self.contexts.append(context.tenant_id)
        return self.subscription


class UnavailableSource:
    async def subscribe(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunEventNotificationSubscription:
        del context, run_id
        raise RunEventNotificationUnavailable("redis unavailable")


def record(sequence_no: int, event_type: str) -> RunEventRecord:
    payload: dict[str, JsonValue]
    if event_type == "run_created":
        payload = {
            "deployment_id": "66666666-6666-4666-8666-666666666666",
            "snapshot_id": "77777777-7777-4777-8777-777777777777",
        }
    elif event_type == "text_delta":
        payload = {"message_id": str(MESSAGE_ID), "delta": "hello"}
    else:
        payload = {
            "result_message_id": str(MESSAGE_ID),
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "reasoning_tokens": 0,
                "estimated": True,
            },
            "warnings": [],
            "result_quality": "NORMAL",
        }
    return RunEventRecord(
        id=UUID(int=sequence_no),
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        session_id=SESSION_ID,
        sequence_no=sequence_no,
        source_event_id=f"source-{sequence_no}",
        execution_attempt=1,
        schema_version="1.0",
        event_type=event_type,
        payload_version="1.0",
        payload=payload,
        occurred_at=NOW,
        recorded_at=NOW,
        trace_id="trace-sse",
    )


def query_service(store: MutableEventStore) -> RunEventQueryService:
    return RunEventQueryService(Resolver(), cast(RunEventQueryStore, store))


async def collect(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([frame async for frame in stream])


@pytest.mark.asyncio
async def test_stream_subscribes_before_history_and_closes_after_terminal() -> None:
    store = MutableEventStore([record(1, "run_created"), record(2, "run_succeeded")])
    subscription = Subscription(lambda: None)
    source = Source(subscription)
    service = RunEventStreamService(
        query_service(store), cast(RunEventNotificationSource, source)
    )

    stream = await service.open_stream(
        principal(), run_id=str(RUN_ID), after=0, metadata=METADATA
    )
    payload = await collect(stream)

    assert source.contexts == [str(TENANT_ID)]
    assert payload.count(b"event: run_event") == 2
    assert b"id: 1\n" in payload and b"id: 2\n" in payload
    assert subscription.waits == 0
    assert subscription.closed is True


@pytest.mark.asyncio
async def test_stream_uses_notification_only_to_wake_postgresql_read() -> None:
    store = MutableEventStore([record(1, "run_created")])

    def append_terminal() -> None:
        if len(store.events) == 1:
            store.events.append(record(2, "run_succeeded"))

    subscription = Subscription(append_terminal)
    service = RunEventStreamService(
        query_service(store),
        cast(RunEventNotificationSource, Source(subscription)),
        heartbeat_seconds=0.1,
    )

    stream = await service.open_stream(
        principal(), run_id=str(RUN_ID), after=0, metadata=METADATA
    )
    payload = await collect(stream)

    assert store.reads == [0, 1]
    assert payload.count(b"event: run_event") == 2
    assert subscription.waits == 1


@pytest.mark.asyncio
async def test_stream_polls_postgresql_when_notification_source_is_unavailable() -> (
    None
):
    store = MutableEventStore([record(1, "run_created")])
    sleeps = 0

    async def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        store.events.append(record(2, "run_succeeded"))

    metrics = PlatformMetrics()
    service = RunEventStreamService(
        query_service(store),
        cast(RunEventNotificationSource, UnavailableSource()),
        poll_interval_seconds=0.1,
        sleep=sleep,
        metrics=metrics,
    )

    stream = await service.open_stream(
        principal(), run_id=str(RUN_ID), after=0, metadata=METADATA
    )
    payload = await collect(stream)

    assert sleeps == 1
    assert b": heartbeat\n\n" in payload
    assert payload.count(b"event: run_event") == 2
    metrics_payload = generate_latest(metrics.registry).decode()
    assert 'outcome="notification_fallback"' in metrics_payload
    assert 'outcome="terminal"' in metrics_payload


@pytest.mark.asyncio
async def test_stream_records_connection_frames_and_fallback_metrics() -> None:
    store = MutableEventStore([record(1, "run_created"), record(2, "run_succeeded")])
    metrics = PlatformMetrics()
    service = RunEventStreamService(query_service(store), metrics=metrics)

    stream = await service.open_stream(
        principal(), run_id=str(RUN_ID), after=0, metadata=METADATA
    )
    await collect(stream)
    payload = generate_latest(metrics.registry).decode()

    assert "agent_platform_sse_connections 0.0" in payload
    assert 'frame_type="run_event"' in payload
    assert 'outcome="terminal"' in payload


@pytest.mark.asyncio
async def test_stream_counts_connection_only_while_generator_is_running() -> None:
    store = MutableEventStore([record(1, "run_created"), record(2, "run_succeeded")])
    metrics = PlatformMetrics()
    service = RunEventStreamService(query_service(store), metrics=metrics)

    stream = await service.open_stream(
        principal(), run_id=str(RUN_ID), after=0, metadata=METADATA
    )
    assert metrics.registry.get_sample_value("agent_platform_sse_connections") == 0

    await anext(stream)
    assert metrics.registry.get_sample_value("agent_platform_sse_connections") == 1

    await cast(AsyncGenerator[bytes, None], stream).aclose()
    assert metrics.registry.get_sample_value("agent_platform_sse_connections") == 0
