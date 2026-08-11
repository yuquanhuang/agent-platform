"""Redis Pub/Sub adapters; PostgreSQL remains the RunEvent source of truth."""

import json
from collections.abc import Awaitable
from typing import Protocol
from uuid import UUID

from redis.exceptions import RedisError

from packages.application.event_service import (
    RunEventNotification,
    RunEventNotificationPublisher,
    RunEventNotificationSource,
    RunEventNotificationSubscription,
    RunEventNotificationUnavailable,
)
from packages.application.outbox import RetryableOutboxError
from packages.contracts.public import TenantContext

CHANNEL_PREFIX = "agent-platform:v1:run-events"


class AsyncRedisPubSub(Protocol):
    async def subscribe(self, *channels: str) -> None: ...

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float = 0.0
    ) -> dict[str, object] | None: ...

    async def aclose(self) -> None: ...


class AsyncRedisClient(Protocol):
    def publish(self, channel: str, message: str) -> Awaitable[int]: ...

    def pubsub(self, **kwargs: object) -> AsyncRedisPubSub: ...


class RedisRunEventNotificationPublisher(RunEventNotificationPublisher):
    """Publish only a wake-up envelope; subscribers must read facts from PostgreSQL."""

    def __init__(self, redis: AsyncRedisClient) -> None:
        self._redis = redis

    async def publish(self, notification: RunEventNotification) -> None:
        channel = run_event_notification_channel(
            notification.tenant_id, notification.run_id
        )
        payload = json.dumps(
            {
                "notification_id": str(notification.notification_id),
                "tenant_id": str(notification.tenant_id),
                "run_id": str(notification.run_id),
                "first_sequence_no": notification.first_sequence_no,
                "last_sequence_no": notification.last_sequence_no,
                "event_count": notification.event_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            await self._redis.publish(channel, payload)
        except RedisError as error:
            raise RetryableOutboxError(
                "Redis notification publish is unavailable"
            ) from error


class _RedisRunEventSubscription(RunEventNotificationSubscription):
    def __init__(self, pubsub: AsyncRedisPubSub) -> None:
        self._pubsub = pubsub

    async def wait(self, *, timeout_seconds: float) -> bool:
        try:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=timeout_seconds,
            )
        except RedisError as error:
            raise RunEventNotificationUnavailable(
                "Redis notification subscription is unavailable"
            ) from error
        return message is not None

    async def aclose(self) -> None:
        try:
            await self._pubsub.aclose()
        except RedisError as error:
            raise RunEventNotificationUnavailable(
                "Redis notification subscription close failed"
            ) from error


class RedisRunEventNotificationSource(RunEventNotificationSource):
    def __init__(self, redis: AsyncRedisClient) -> None:
        self._redis = redis

    async def subscribe(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunEventNotificationSubscription:
        channel = run_event_notification_channel(UUID(context.tenant_id), run_id)
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(channel)
        except RedisError as error:
            await _close_quietly(pubsub)
            raise RunEventNotificationUnavailable(
                "Redis notification subscription is unavailable"
            ) from error
        return _RedisRunEventSubscription(pubsub)


def run_event_notification_channel(tenant_id: UUID, run_id: UUID) -> str:
    return f"{CHANNEL_PREFIX}:{tenant_id}:{run_id}"


async def _close_quietly(pubsub: AsyncRedisPubSub) -> None:
    try:
        await pubsub.aclose()
    except RedisError:
        return
