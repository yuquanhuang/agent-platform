"""Redis wake-up adapter tests."""

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from redis.exceptions import RedisError

from packages.application.artifacts import (
    ArtifactDownloadRevocationNotification,
    ArtifactDownloadRevocationUnavailable,
)
from packages.application.event_service import (
    RunEventNotification,
    RunEventNotificationUnavailable,
)
from packages.application.outbox import RetryableOutboxError
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.redis.artifact_downloads import (
    RedisArtifactDownloadRevocationPublisher,
    RedisArtifactDownloadRevocationSource,
    artifact_download_revocation_channel,
)
from packages.infrastructure.redis.events import (
    AsyncRedisClient,
    AsyncRedisPubSub,
    RedisRunEventNotificationPublisher,
    RedisRunEventNotificationSource,
    run_event_notification_channel,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
NOTIFICATION_ID = UUID("44444444-4444-4444-8444-444444444444")
ARTIFACT_ID = UUID("66666666-6666-4666-8666-666666666666")


class FakePubSub:
    def __init__(self) -> None:
        self.channels: list[str] = []
        self.subscription_acknowledged = False
        self.message: dict[str, object] | None = {"type": "message"}
        self.error: RedisError | None = None
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        if self.error is not None:
            raise self.error
        self.channels.extend(channels)

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float = 0.0
    ) -> dict[str, object] | None:
        assert timeout > 0
        if not ignore_subscribe_messages and not self.subscription_acknowledged:
            self.subscription_acknowledged = True
            return {"type": "subscribe"}
        assert ignore_subscribe_messages is True
        if self.error is not None:
            raise self.error
        return self.message

    async def aclose(self) -> None:
        self.closed = True
        if self.error is not None:
            raise self.error


class FakeRedis:
    def __init__(self, pubsub: FakePubSub | None = None) -> None:
        self.subscription = pubsub or FakePubSub()
        self.published: list[tuple[str, str]] = []
        self.publish_error: RedisError | None = None

    async def publish(self, channel: str, message: str) -> int:
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((channel, message))
        return 1

    def pubsub(self, **kwargs: object) -> AsyncRedisPubSub:
        assert kwargs == {"ignore_subscribe_messages": False}
        return cast(AsyncRedisPubSub, self.subscription)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="55555555-5555-4555-8555-555555555555",
        auth_time=datetime(2026, 8, 9, tzinfo=UTC),
        request_id="req-redis-events",
        trace_id="trace-redis-events",
    )


@pytest.mark.asyncio
async def test_publisher_uses_tenant_run_channel_and_json_wakeup_envelope() -> None:
    redis = FakeRedis()
    publisher = RedisRunEventNotificationPublisher(cast(AsyncRedisClient, redis))

    await publisher.publish(
        RunEventNotification(
            notification_id=NOTIFICATION_ID,
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            first_sequence_no=3,
            last_sequence_no=4,
            event_count=2,
        )
    )

    channel, raw_payload = redis.published[0]
    assert channel == run_event_notification_channel(TENANT_ID, RUN_ID)
    assert channel != run_event_notification_channel(OTHER_TENANT_ID, RUN_ID)
    assert json.loads(raw_payload) == {
        "event_count": 2,
        "first_sequence_no": 3,
        "last_sequence_no": 4,
        "notification_id": str(NOTIFICATION_ID),
        "run_id": str(RUN_ID),
        "tenant_id": str(TENANT_ID),
    }


@pytest.mark.asyncio
async def test_publisher_converts_redis_failure_to_retryable_outbox_error() -> None:
    redis = FakeRedis()
    redis.publish_error = RedisError("down")
    publisher = RedisRunEventNotificationPublisher(cast(AsyncRedisClient, redis))

    with pytest.raises(RetryableOutboxError, match="publish is unavailable"):
        await publisher.publish(
            RunEventNotification(
                notification_id=NOTIFICATION_ID,
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                first_sequence_no=1,
                last_sequence_no=1,
                event_count=1,
            )
        )


@pytest.mark.asyncio
async def test_source_subscribes_to_isolated_channel_waits_and_closes() -> None:
    redis = FakeRedis()
    source = RedisRunEventNotificationSource(cast(AsyncRedisClient, redis))

    subscription = await source.subscribe(context(), run_id=RUN_ID)

    assert redis.subscription.channels == [
        run_event_notification_channel(TENANT_ID, RUN_ID)
    ]
    assert await subscription.wait(timeout_seconds=0.1) is True
    redis.subscription.message = None
    assert await subscription.wait(timeout_seconds=0.1) is False
    await subscription.aclose()
    assert redis.subscription.closed is True


@pytest.mark.asyncio
async def test_source_converts_subscribe_and_wait_failures_to_unavailable() -> None:
    subscribe_pubsub = FakePubSub()
    subscribe_pubsub.error = RedisError("subscribe down")
    subscribe_source = RedisRunEventNotificationSource(
        cast(AsyncRedisClient, FakeRedis(subscribe_pubsub))
    )

    with pytest.raises(RunEventNotificationUnavailable, match="subscription"):
        await subscribe_source.subscribe(context(), run_id=RUN_ID)
    assert subscribe_pubsub.closed is True

    wait_pubsub = FakePubSub()
    wait_source = RedisRunEventNotificationSource(
        cast(AsyncRedisClient, FakeRedis(wait_pubsub))
    )
    subscription = await wait_source.subscribe(context(), run_id=RUN_ID)
    wait_pubsub.error = RedisError("wait down")
    with pytest.raises(RunEventNotificationUnavailable, match="subscription"):
        await subscription.wait(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_artifact_revocation_uses_tenant_artifact_channel() -> None:
    redis = FakeRedis()
    publisher = RedisArtifactDownloadRevocationPublisher(cast(AsyncRedisClient, redis))
    revoked_at = datetime(2026, 8, 11, tzinfo=UTC)

    await publisher.publish(
        ArtifactDownloadRevocationNotification(
            notification_id=NOTIFICATION_ID,
            tenant_id=TENANT_ID,
            artifact_id=ARTIFACT_ID,
            revoked_at=revoked_at,
            grant_count=2,
        )
    )

    channel, raw_payload = redis.published[0]
    assert channel == artifact_download_revocation_channel(TENANT_ID, ARTIFACT_ID)
    assert json.loads(raw_payload) == {
        "artifact_id": str(ARTIFACT_ID),
        "grant_count": 2,
        "notification_id": str(NOTIFICATION_ID),
        "revoked_at": revoked_at.isoformat(),
        "tenant_id": str(TENANT_ID),
    }


@pytest.mark.asyncio
async def test_artifact_revocation_source_fails_closed_to_polling_boundary() -> None:
    pubsub = FakePubSub()
    source = RedisArtifactDownloadRevocationSource(
        cast(AsyncRedisClient, FakeRedis(pubsub))
    )
    subscription = await source.subscribe(
        tenant_id=TENANT_ID,
        artifact_id=ARTIFACT_ID,
    )

    assert pubsub.channels == [
        artifact_download_revocation_channel(TENANT_ID, ARTIFACT_ID)
    ]
    pubsub.error = RedisError("wait down")
    with pytest.raises(ArtifactDownloadRevocationUnavailable, match="subscription"):
        await subscription.wait(timeout_seconds=0.1)
