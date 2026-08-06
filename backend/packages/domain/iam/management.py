"""IAM management records and concurrency helpers."""

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import JsonValue

from packages.contracts.public import TenantContext

ETAG_PATTERN = re.compile(r'^"rv:([1-9][0-9]*)"$')
ResourceStatus = Literal["ACTIVE", "DISABLED", "DELETING", "DELETED"]
OperationStatus = Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


@dataclass(frozen=True, slots=True)
class TenantRecord:
    id: UUID
    code: str
    name: str
    status: ResourceStatus
    resource_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemberRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    display_name: str
    email: str | None
    role_ids: tuple[UUID, ...]
    status: ResourceStatus
    membership_version: int
    resource_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RoleRecord:
    id: UUID
    tenant_id: UUID
    code: str
    name: str
    description: str | None
    permissions: tuple[str, ...]
    status: ResourceStatus
    built_in: bool
    resource_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    id: UUID
    operation_type: str
    status: OperationStatus
    resource_type: str | None
    resource_id: UUID | None
    result: dict[str, JsonValue] | None
    error: dict[str, JsonValue] | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TenantAccess:
    context: TenantContext
    permissions: frozenset[str]

    def allows(self, resource: str, action: str) -> bool:
        return f"{resource}:{action}" in self.permissions


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    response_status: int
    response_body: dict[str, object]
    response_etag: str | None


@dataclass(frozen=True, slots=True)
class MutationOutcome[RecordT]:
    value: RecordT | None = None
    replay: IdempotencyReplay | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.replay is None):
            raise ValueError("mutation outcome requires exactly one result")


def format_etag(resource_version: int) -> str:
    if resource_version < 1:
        raise ValueError("resource_version must be positive")
    return f'"rv:{resource_version}"'


def parse_etag(value: str) -> int:
    match = ETAG_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("If-Match must use the frozen strong ETag format")
    return int(match.group(1))


def encode_cursor(created_at: datetime, resource_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(resource_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded: object = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(decoded, dict):
            raise TypeError
        payload = cast(dict[str, object], decoded)
        created_at_value = payload["created_at"]
        resource_id_value = payload["id"]
        if not isinstance(created_at_value, str) or not isinstance(
            resource_id_value, str
        ):
            raise TypeError
        created_at = datetime.fromisoformat(created_at_value)
        resource_id = UUID(resource_id_value)
        if created_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    return created_at, resource_id
