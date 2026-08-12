"""Real Redis RunEvent notification wake-up verification."""

import os
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import pytest
from redis.asyncio import Redis

from packages.application.artifacts import ArtifactDownloadRevocationNotification
from packages.application.event_service import RunEventNotification
from packages.application.outbox import RetryableOutboxError
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.redis.artifact_downloads import (
    RedisArtifactDownloadRevocationPublisher,
    RedisArtifactDownloadRevocationSource,
)
from packages.infrastructure.redis.events import (
    AsyncRedisClient,
    RedisRunEventNotificationPublisher,
    RedisRunEventNotificationSource,
)

REDIS_URL_ENV = "AP_TEST_REDIS_URL"
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
ARTIFACT_ID = UUID("77777777-7777-4777-8777-777777777777")


def context(tenant_id: UUID) -> TenantContext:
    return TenantContext(
        tenant_id=str(tenant_id),
        subject_type=SubjectType.SERVICE,
        subject_id="44444444-4444-4444-8444-444444444444",
        auth_time=datetime(2026, 8, 9, tzinfo=UTC),
        request_id="req-real-redis",
        trace_id="trace-real-redis",
    )


@pytest.mark.asyncio
async def test_real_redis_wakes_only_the_target_tenant_run() -> None:
    redis_url = os.getenv(REDIS_URL_ENV)
    if redis_url is None:
        pytest.skip(f"{REDIS_URL_ENV} is not configured")
    parsed_url = urlparse(redis_url)
    database = int(parsed_url.path.removeprefix("/") or "0")
    redis = Redis(
        host=parsed_url.hostname or "localhost",
        port=parsed_url.port or 6379,
        db=database,
        username=parsed_url.username,
        password=parsed_url.password,
        ssl=parsed_url.scheme == "rediss",
    )
    redis_client = cast(AsyncRedisClient, redis)
    source = RedisRunEventNotificationSource(redis_client)
    target = await source.subscribe(context(TENANT_ID), run_id=RUN_ID)
    other_tenant = await source.subscribe(context(OTHER_TENANT_ID), run_id=RUN_ID)
    try:
        await RedisRunEventNotificationPublisher(redis_client).publish(
            RunEventNotification(
                notification_id=UUID("55555555-5555-4555-8555-555555555555"),
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                first_sequence_no=7,
                last_sequence_no=9,
                event_count=3,
            )
        )

        assert await target.wait(timeout_seconds=1.0) is True
        assert await other_tenant.wait(timeout_seconds=0.1) is False
    finally:
        await target.aclose()
        await other_tenant.aclose()
        await redis.aclose()


@pytest.mark.asyncio
async def test_real_redis_outage_is_classified_as_retryable() -> None:
    if os.getenv("AP_TEST_REDIS_EXPECT_UNAVAILABLE") != "1":
        pytest.skip("AP_TEST_REDIS_EXPECT_UNAVAILABLE=1 is required")
    redis_url = os.getenv(REDIS_URL_ENV)
    if redis_url is None:
        pytest.skip(f"{REDIS_URL_ENV} is not configured")
    parsed_url = urlparse(redis_url)
    redis = Redis(
        host=parsed_url.hostname or "localhost",
        port=parsed_url.port or 6379,
        db=int(parsed_url.path.removeprefix("/") or "0"),
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        with pytest.raises(RetryableOutboxError):
            await RedisRunEventNotificationPublisher(
                cast(AsyncRedisClient, redis)
            ).publish(
                RunEventNotification(
                    notification_id=UUID("66666666-6666-4666-8666-666666666666"),
                    tenant_id=TENANT_ID,
                    run_id=RUN_ID,
                    first_sequence_no=1,
                    last_sequence_no=1,
                    event_count=1,
                )
            )
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_real_redis_wakes_only_target_artifact_streams() -> None:
    redis_url = os.getenv(REDIS_URL_ENV)
    if redis_url is None:
        pytest.skip(f"{REDIS_URL_ENV} is not configured")
    parsed_url = urlparse(redis_url)
    redis = Redis(
        host=parsed_url.hostname or "localhost",
        port=parsed_url.port or 6379,
        db=int(parsed_url.path.removeprefix("/") or "0"),
        username=parsed_url.username,
        password=parsed_url.password,
        ssl=parsed_url.scheme == "rediss",
    )
    redis_client = cast(AsyncRedisClient, redis)
    source = RedisArtifactDownloadRevocationSource(redis_client)
    target = await source.subscribe(
        tenant_id=TENANT_ID,
        artifact_id=ARTIFACT_ID,
    )
    other_tenant = await source.subscribe(
        tenant_id=OTHER_TENANT_ID,
        artifact_id=ARTIFACT_ID,
    )
    try:
        await RedisArtifactDownloadRevocationPublisher(redis_client).publish(
            ArtifactDownloadRevocationNotification(
                notification_id=UUID("88888888-8888-4888-8888-888888888888"),
                tenant_id=TENANT_ID,
                artifact_id=ARTIFACT_ID,
                revoked_at=datetime.now(UTC),
                grant_count=1,
            )
        )
        assert await target.wait(timeout_seconds=1.0) is True
        assert await other_tenant.wait(timeout_seconds=0.1) is False
    finally:
        await target.aclose()
        await other_tenant.aclose()
        await redis.aclose()
