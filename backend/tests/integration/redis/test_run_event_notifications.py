"""Real Redis RunEvent notification wake-up verification."""

import os
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import pytest
from redis.asyncio import Redis

from packages.application.event_service import RunEventNotification
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.redis.events import (
    AsyncRedisClient,
    RedisRunEventNotificationPublisher,
    RedisRunEventNotificationSource,
)

REDIS_URL_ENV = "AP_TEST_REDIS_URL"
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


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
