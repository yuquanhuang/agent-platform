"""Redis wake-ups for active Artifact download revocation."""

import json
from uuid import UUID

from redis.exceptions import RedisError

from packages.application.artifacts import (
    ArtifactDownloadRevocationNotification,
    ArtifactDownloadRevocationPublisher,
    ArtifactDownloadRevocationSource,
    ArtifactDownloadRevocationSubscription,
    ArtifactDownloadRevocationUnavailable,
)
from packages.application.outbox import RetryableOutboxError
from packages.infrastructure.redis.events import AsyncRedisClient, AsyncRedisPubSub

CHANNEL_PREFIX = "agent-platform:v1:artifact-download-revocations"


class RedisArtifactDownloadRevocationPublisher(ArtifactDownloadRevocationPublisher):
    def __init__(self, redis: AsyncRedisClient) -> None:
        self._redis = redis

    async def publish(
        self, notification: ArtifactDownloadRevocationNotification
    ) -> None:
        channel = artifact_download_revocation_channel(
            notification.tenant_id, notification.artifact_id
        )
        payload = json.dumps(
            {
                "notification_id": str(notification.notification_id),
                "tenant_id": str(notification.tenant_id),
                "artifact_id": str(notification.artifact_id),
                "revoked_at": notification.revoked_at.isoformat(),
                "grant_count": notification.grant_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            await self._redis.publish(channel, payload)
        except RedisError as error:
            raise RetryableOutboxError(
                "Redis Artifact revocation publish is unavailable"
            ) from error


class _RedisArtifactDownloadRevocationSubscription(
    ArtifactDownloadRevocationSubscription
):
    def __init__(self, pubsub: AsyncRedisPubSub) -> None:
        self._pubsub = pubsub

    async def wait(self, *, timeout_seconds: float) -> bool:
        try:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=timeout_seconds,
            )
        except RedisError as error:
            raise ArtifactDownloadRevocationUnavailable(
                "Redis Artifact revocation subscription is unavailable"
            ) from error
        return message is not None

    async def aclose(self) -> None:
        try:
            await self._pubsub.aclose()
        except RedisError as error:
            raise ArtifactDownloadRevocationUnavailable(
                "Redis Artifact revocation subscription close failed"
            ) from error


class RedisArtifactDownloadRevocationSource(ArtifactDownloadRevocationSource):
    def __init__(self, redis: AsyncRedisClient) -> None:
        self._redis = redis

    async def subscribe(
        self,
        *,
        tenant_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactDownloadRevocationSubscription:
        channel = artifact_download_revocation_channel(tenant_id, artifact_id)
        pubsub = self._redis.pubsub(ignore_subscribe_messages=False)
        try:
            await pubsub.subscribe(channel)
            confirmation = await pubsub.get_message(
                ignore_subscribe_messages=False,
                timeout=1.0,
            )
            if confirmation is None or confirmation.get("type") != "subscribe":
                raise RedisError("Redis subscription acknowledgement was not received")
        except RedisError as error:
            await _close_quietly(pubsub)
            raise ArtifactDownloadRevocationUnavailable(
                "Redis Artifact revocation subscription is unavailable"
            ) from error
        return _RedisArtifactDownloadRevocationSubscription(pubsub)


def artifact_download_revocation_channel(tenant_id: UUID, artifact_id: UUID) -> str:
    return f"{CHANNEL_PREFIX}:{tenant_id}:{artifact_id}"


async def _close_quietly(pubsub: AsyncRedisPubSub) -> None:
    try:
        await pubsub.aclose()
    except RedisError:
        return
