"""Bounded Redis client construction from a resolved secret DSN."""

from typing import Protocol, cast

from pydantic import SecretStr
from redis.asyncio import Redis


class ManagedRedisClient(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


def create_redis_client(
    resolved_dsn: SecretStr,
    *,
    connect_timeout_seconds: float = 5.0,
    socket_timeout_seconds: float = 5.0,
) -> ManagedRedisClient:
    if connect_timeout_seconds <= 0 or socket_timeout_seconds <= 0:
        raise ValueError("Redis timeouts must be positive")
    dsn = resolved_dsn.get_secret_value()
    if not dsn.startswith(("redis://", "rediss://")):
        raise ValueError("Redis DSN must use redis:// or rediss://")
    return cast(
        ManagedRedisClient,
        Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            dsn,
            socket_connect_timeout=connect_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=30,
            retry_on_timeout=False,
        ),
    )
