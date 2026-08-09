"""Read-only Message history use case for owned Sessions."""

from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import (
    Message,
    MessageContentPart,
    MessagePage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import MessageRecord, TenantAccess


class MessageHistoryAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class MessageHistoryStore(Protocol):
    async def list_session_messages(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        limit: int,
        cursor: str | None,
        branch_id: UUID | None,
    ) -> tuple[list[MessageRecord], str | None] | None: ...


class MessageHistoryService:
    """Authorize immutable Message history reads for the current Session owner."""

    def __init__(
        self,
        access_resolver: MessageHistoryAccessResolver,
        store: MessageHistoryStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_session_messages(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        limit: int,
        cursor: str | None,
        branch_id: str | None,
        metadata: RequestMetadata,
    ) -> MessagePage:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("session", "read") or not access.allows("message", "list"):
            raise permission_denied()
        result = await self._store.list_session_messages(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            session_id=_resource_id(session_id),
            limit=limit,
            cursor=cursor,
            branch_id=_optional_resource_id(branch_id),
        )
        if result is None:
            raise resource_not_found()
        records, next_cursor = result
        return MessagePage(
            items=[_message(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )


def _message(record: MessageRecord) -> Message:
    return Message(
        id=str(record.id),
        session_id=str(record.session_id),
        branch_id=str(record.branch_id) if record.branch_id is not None else None,
        parent_message_id=(
            str(record.parent_message_id)
            if record.parent_message_id is not None
            else None
        ),
        role=record.role,
        content_parts=[
            MessageContentPart(
                type=part.type,
                text=part.text,
                artifact_id=part.artifact_id,
                tool_call_id=part.tool_call_id,
                error_code=part.error_code,
            )
            for part in record.content_parts
        ],
        source_run_id=(
            str(record.source_run_id) if record.source_run_id is not None else None
        ),
        created_at=record.created_at,
    )


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _optional_resource_id(value: str | None) -> UUID | None:
    return _resource_id(value) if value is not None else None
