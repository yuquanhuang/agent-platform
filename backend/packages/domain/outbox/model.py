"""Transaction Outbox domain values and retry policy."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Tenant-scoped immutable message plus mutable delivery state snapshot."""

    id: UUID
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: Mapping[str, object]
    payload_schema_version: int
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.aggregate_type or len(self.aggregate_type) > 64:
            raise ValueError("aggregate_type must contain 1 to 64 characters")
        if not self.event_type or len(self.event_type) > 128:
            raise ValueError("event_type must contain 1 to 128 characters")
        if self.payload_schema_version < 1:
            raise ValueError("payload_schema_version must be positive")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")
        if self.next_attempt_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("outbox timestamps must be timezone-aware")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


def retry_delay(
    attempts: int,
    *,
    initial: timedelta = timedelta(seconds=1),
    maximum: timedelta = timedelta(minutes=5),
) -> timedelta:
    """Return bounded deterministic exponential backoff for one failed attempt."""

    if attempts < 1:
        raise ValueError("attempts must be positive")
    if initial <= timedelta(0) or maximum < initial:
        raise ValueError("retry delay bounds are invalid")
    multiplier = 2 ** min(attempts - 1, 30)
    return min(initial * multiplier, maximum)
