"""Frozen Audit query use case with tenant authorization."""

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.contracts.generated.resources_models import AuditPage, AuditRecord
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    validation_error,
)
from packages.domain.public import AuditLogRecord, TenantAccess


class AuditAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class AuditQueryStore(Protocol):
    async def list_audit_logs(
        self,
        context: TenantContext,
        *,
        action: str | None,
        resource_type: str | None,
        actor_id: UUID | None,
        run_id: UUID | None,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[AuditLogRecord], str | None]: ...


class AuditManagementService:
    """Return the frozen minimal Audit projection without metadata or digests."""

    def __init__(
        self, access_resolver: AuditAccessResolver, store: AuditQueryStore
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_audit_logs(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action: str | None,
        resource_type: str | None,
        actor_id: str | None,
        run_id: str | None,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> AuditPage:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("audit", "list"):
            raise permission_denied()
        normalized_from = _utc_datetime(occurred_from, "occurred_from")
        normalized_to = _utc_datetime(occurred_to, "occurred_to")
        if (
            normalized_from is not None
            and normalized_to is not None
            and normalized_from > normalized_to
        ):
            raise validation_error("occurred_from must not be after occurred_to.")
        records, next_cursor = await self._store.list_audit_logs(
            access.context,
            action=action,
            resource_type=resource_type,
            actor_id=_optional_uuid(actor_id, "actor_id"),
            run_id=_optional_uuid(run_id, "run_id"),
            occurred_from=normalized_from,
            occurred_to=normalized_to,
            limit=limit,
            cursor=cursor,
        )
        return AuditPage(
            items=[_audit_record(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )


def _audit_record(record: AuditLogRecord) -> AuditRecord:
    return AuditRecord(
        event_id=str(record.event_id),
        occurred_at=record.occurred_at,
        actor_id=str(record.actor_id),
        action=record.action,
        resource_type=record.resource_type,
        resource_id=str(record.resource_id) if record.resource_id is not None else "",
        result=record.result,
        trace_id=record.trace_id,
    )


def _optional_uuid(value: str | None, field: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as error:
        raise validation_error(f"{field} must be a UUID.") from error


def _utc_datetime(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise validation_error(f"{field} must include a timezone offset.")
    return value.astimezone(UTC)
