"""Session management use cases and user-ownership boundary."""

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.generated.core_models import (
    OperationAccepted,
    Session,
    SessionCreateRequest,
    SessionMetadata,
    SessionPage,
    SessionUpdateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    MutationOutcome,
    OperationRecord,
    SessionRecord,
    TenantAccess,
    format_etag,
    parse_etag,
)


class SessionAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class SessionStore(Protocol):
    async def list_sessions(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        limit: int,
        cursor: str | None,
        agent_id: UUID | None,
        status: str | None,
    ) -> tuple[list[SessionRecord], str | None]: ...

    async def create_session(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        request: SessionCreateRequest,
        metadata_value: dict[str, JsonValue],
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[SessionRecord]: ...

    async def get_session(
        self, context: TenantContext, *, user_id: UUID, session_id: UUID
    ) -> SessionRecord | None: ...

    async def update_session(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        expected_version: int,
        request: SessionUpdateRequest,
        metadata_value: dict[str, JsonValue] | None,
        metadata: RequestMetadata,
    ) -> SessionRecord | None: ...

    async def archive_session(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[SessionRecord] | None: ...

    async def delete_session(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord] | None: ...


class SessionManagementService:
    """Authorize Session operations and always scope them to the current user."""

    def __init__(
        self, access_resolver: SessionAccessResolver, store: SessionStore
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_sessions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        agent_id: str | None,
        status: str | None,
        metadata: RequestMetadata,
    ) -> SessionPage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._store.list_sessions(
            access.context,
            user_id=_actor_id(access.context),
            limit=limit,
            cursor=cursor,
            agent_id=_optional_resource_id(agent_id),
            status=status,
        )
        return SessionPage(
            items=[_session(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_session(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: SessionCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Session, str]:
        if "title" in request.model_fields_set and request.title is None:
            raise validation_error("Session title cannot be null when creating.")
        access = await self._access(principal, "create", metadata)
        metadata_value = _metadata_value(request.metadata)
        outcome = await self._store.create_session(
            access.context,
            user_id=_actor_id(access.context),
            request=request,
            metadata_value=metadata_value,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("session.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Session, _session)

    async def get_session(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Session, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._store.get_session(
            access.context,
            user_id=_actor_id(access.context),
            session_id=_resource_id(session_id),
        )
        if record is None:
            raise resource_not_found()
        return _session(record), format_etag(record.resource_version)

    async def update_session(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        if_match: str,
        request: SessionUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Session, str]:
        if not request.model_fields_set:
            raise validation_error("Session update requires at least one field.")
        if "metadata" in request.model_fields_set and request.metadata is None:
            raise validation_error("Session metadata cannot be null.")
        access = await self._access(principal, "update", metadata)
        record = await self._store.update_session(
            access.context,
            user_id=_actor_id(access.context),
            session_id=_resource_id(session_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata_value=(
                _metadata_value(request.metadata)
                if "metadata" in request.model_fields_set
                else None
            ),
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _session(record), format_etag(record.resource_version)

    async def archive_session(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Session, str]:
        access = await self._access(principal, "update", metadata)
        outcome = await self._store.archive_session(
            access.context,
            user_id=_actor_id(access.context),
            session_id=_resource_id(session_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "session.archive",
                None,
                extra={"session_id": session_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Session, _session)

    async def delete_session(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "delete", metadata)
        outcome = await self._store.delete_session(
            access.context,
            user_id=_actor_id(access.context),
            session_id=_resource_id(session_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "session.delete",
                None,
                extra={"session_id": session_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return OperationAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Session delete outcome is missing its operation")
        return OperationAccepted(
            operation_id=str(outcome.value.id),
            status="ACCEPTED",
            status_url=f"/api/v1/operations/{outcome.value.id}",
        )

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("session", action):
            raise permission_denied()
        return access


def _session(record: SessionRecord) -> Session:
    return Session(
        id=str(record.id),
        agent_id=str(record.agent_id),
        user_id=str(record.user_id),
        default_deployment_id=str(record.default_deployment_id),
        title=record.title,
        status=record.status,
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        archived_at=record.archived_at,
    )


def _versioned_outcome[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        if outcome.replay.response_etag is None:
            raise RuntimeError("Session replay is missing ETag")
        return (
            model_type.model_validate(outcome.replay.response_body),
            outcome.replay.response_etag,
        )
    if outcome.value is None:
        raise RuntimeError("Session mutation outcome is missing its value")
    version = getattr(outcome.value, "resource_version", None)
    if not isinstance(version, int):
        raise TypeError("Session mutation outcome is missing resource_version")
    return mapper(outcome.value), format_etag(version)


def _metadata_value(value: SessionMetadata | None) -> dict[str, JsonValue]:
    if value is None:
        return {}
    metadata = value.model_dump(mode="json", exclude_unset=True)
    tags_value = metadata.get("tags")
    tags = cast(list[object], tags_value) if isinstance(tags_value, list) else []
    if any(not isinstance(tag, str) or len(tag) > 32 for tag in tags):
        raise validation_error("Session metadata tags must not exceed 32 characters.")
    return metadata


def _request_hash(
    operation_type: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation_type, request, extra=extra)


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _optional_resource_id(value: str | None) -> UUID | None:
    return _resource_id(value) if value is not None else None


def _expected_version(value: str) -> int:
    try:
        return parse_etag(value)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _actor_id(context: TenantContext) -> UUID:
    return _resource_id(context.subject_id)
