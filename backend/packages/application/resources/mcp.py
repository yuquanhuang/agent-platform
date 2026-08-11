"""MCP resource governance and immutable capability publication use cases."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.application.resources.mcp_discovery import (
    McpDiscoveryEvidenceStore,
    validate_mcp_content,
)
from packages.application.resources.references import CompositeResourceReferenceReader
from packages.contracts.generated.resource_content import ResourceContentMcp
from packages.contracts.generated.resources_models import (
    ActionRequest,
    McpServerCreateRequest,
    OperationAccepted,
    Resource,
    ResourceCreateRequest,
    ResourceDiff,
    ResourceDiffChangesItem,
    ResourcePage,
    ResourcePublishRequest,
    ResourceReference,
    ResourceReferencePage,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
    ResourceVersion,
    ResourceVersionPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    IdempotencyReplay,
    MutationOutcome,
    OperationRecord,
    ResourceDefinitionRecord,
    ResourceReferenceRecord,
    ResourceVersionRecord,
    TenantAccess,
    canonical_content_hash,
    format_etag,
    parse_etag,
    resource_content_json,
)


class McpAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class McpRegistry(Protocol):
    async def get_idempotency_replay(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        operation_type: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyReplay | None: ...

    async def list_definitions(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[list[ResourceDefinitionRecord], str | None]: ...

    async def create_definition(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ResourceDefinitionRecord]: ...

    async def get_definition(
        self, context: TenantContext, **kwargs: object
    ) -> ResourceDefinitionRecord | None: ...

    async def update_definition(
        self, context: TenantContext, **kwargs: object
    ) -> ResourceDefinitionRecord | None: ...

    async def delete_definition(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[OperationRecord] | None: ...

    async def request_mcp_capability_discovery(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[OperationRecord] | None: ...

    async def publish_version(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ResourceVersionRecord]: ...

    async def set_definition_status(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ResourceDefinitionRecord] | None: ...

    async def rollback_version(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ResourceVersionRecord] | None: ...

    async def list_versions(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[list[ResourceVersionRecord], str | None]: ...

    async def get_version(
        self, context: TenantContext, **kwargs: object
    ) -> ResourceVersionRecord | None: ...


class McpManagementService:
    """Authorize MCP operations and bind publication to discovered capabilities."""

    def __init__(
        self,
        access_resolver: McpAccessResolver,
        registry: McpRegistry,
        reference_reader: CompositeResourceReferenceReader,
        discovery_store: McpDiscoveryEvidenceStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._registry = registry
        self._reference_reader = reference_reader
        self._discoveries = discovery_store

    async def list_mcp_servers(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        keyword: str | None,
        metadata: RequestMetadata,
    ) -> ResourcePage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._registry.list_definitions(
            access.context,
            resource_type="mcp",
            limit=limit,
            cursor=cursor,
            keyword=keyword,
        )
        return ResourcePage(
            items=[_resource(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: McpServerCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "create", metadata)
        _validate(access, request.content)
        generic = ResourceCreateRequest.model_validate(request.model_dump(mode="json"))
        outcome = await self._registry.create_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            request=generic,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("mcp.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Resource, _resource)

    async def get_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._definition(access, _resource_id(resource_id))
        return _resource(record), format_etag(record.resource_version)

    async def update_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if not request.model_fields_set:
            raise validation_error("MCP update requires at least one field.")
        access = await self._access(principal, "update", metadata)
        if request.content is not None:
            if request.content.resource_type != "mcp":
                raise validation_error("MCP content must use resource_type=mcp.")
            _validate(access, request.content)
        record = await self._registry.update_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def delete_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "delete", metadata)
        outcome = await self._registry.delete_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "mcp.delete",
                None,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _operation_outcome(outcome)

    async def discover_mcp_capabilities(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "execute", metadata)
        definition = await self._definition(access, _resource_id(resource_id))
        content = _mcp_content(definition)
        _validate(access, content)
        outcome = await self._registry.request_mcp_capability_discovery(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_id=definition.id,
            expected_resource_version=definition.resource_version,
            content_hash=canonical_content_hash(content),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "mcp.discover",
                None,
                extra={
                    "resource_id": resource_id,
                    "resource_version": definition.resource_version,
                    "content_hash": canonical_content_hash(content),
                },
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _operation_outcome(outcome)

    async def publish_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourcePublishRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "publish", metadata)
        request_hash = _request_hash(
            "mcp.publish", request, extra={"resource_id": resource_id}
        )
        replay = await self._replay(
            access,
            operation_type="mcp.publish",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return ResourceVersion.model_validate(replay.response_body)
        definition = await self._definition_for_publication(
            access, _resource_id(resource_id), request.expected_resource_version
        )
        content = _mcp_content(definition)
        _validate(access, content, publishing=True)
        evidence = await self._discoveries.get_publishable_evidence(
            access.context,
            definition_id=definition.id,
            draft_resource_version=definition.resource_version,
            content_hash=canonical_content_hash(content),
            allowed_tools=tuple(content.allowed_tools or ()),
        )
        if evidence is None:
            raise resource_state_conflict(
                "MCP publication requires passed discovery for the exact draft and tool allowlist."
            )
        outcome = await self._registry.publish_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            resource_id=definition.id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
            mcp_discovery_attestation_id=evidence.id,
        )
        return _outcome_value(outcome, ResourceVersion, _version)

    async def set_mcp_server_enabled(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "disable", metadata)
        operation = "enable" if enabled else "disable"
        outcome = await self._registry.set_definition_status(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"mcp.{operation}",
                request,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def rollback_mcp_server(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourceRollbackRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "rollback", metadata)
        request_hash = _request_hash(
            "mcp.rollback", request, extra={"resource_id": resource_id}
        )
        replay = await self._replay(
            access,
            operation_type="mcp.rollback",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return ResourceVersion.model_validate(replay.response_body)
        definition = await self._definition_for_publication(
            access, _resource_id(resource_id), request.expected_resource_version
        )
        source = await self._registry.get_version(
            access.context,
            resource_type="mcp",
            resource_id=definition.id,
            version_id=_resource_id(request.version_id),
        )
        if source is None:
            raise resource_not_found()
        content = _mcp_content(source)
        _validate(access, content, publishing=True)
        evidence = await self._discoveries.get_published_evidence(
            access.context,
            source_version_id=source.id,
            definition_id=definition.id,
            content_hash=source.content_hash,
            allowed_tools=tuple(content.allowed_tools or ()),
        )
        if evidence is None:
            raise resource_state_conflict(
                "MCP rollback requires immutable discovery evidence from the source version."
            )
        outcome = await self._registry.rollback_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="mcp",
            resource_id=definition.id,
            source_version_id=source.id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
            mcp_discovery_attestation_id=evidence.id,
        )
        if outcome is None:
            raise resource_not_found()
        return _outcome_value(outcome, ResourceVersion, _version)

    async def list_mcp_server_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourceVersionPage:
        access = await self._access(principal, "read", metadata)
        definition = await self._definition(access, _resource_id(resource_id))
        records, next_cursor = await self._registry.list_versions(
            access.context,
            resource_type="mcp",
            resource_id=definition.id,
            limit=limit,
            cursor=cursor,
        )
        return ResourceVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def diff_mcp_server_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        from_version_id: str,
        to_version_id: str,
        metadata: RequestMetadata,
    ) -> ResourceDiff:
        access = await self._access(principal, "read", metadata)
        definition_id = _resource_id(resource_id)
        before = await self._registry.get_version(
            access.context,
            resource_type="mcp",
            resource_id=definition_id,
            version_id=_resource_id(from_version_id),
        )
        after = await self._registry.get_version(
            access.context,
            resource_type="mcp",
            resource_id=definition_id,
            version_id=_resource_id(to_version_id),
        )
        if before is None or after is None:
            raise resource_not_found()
        before_json = resource_content_json(before.content)
        after_json = resource_content_json(after.content)
        keys = sorted(before_json.keys() | after_json.keys())
        changes = [
            ResourceDiffChangesItem(
                path=f"/{key}",
                change_type=(
                    "added"
                    if key not in before_json
                    else "removed" if key not in after_json else "changed"
                ),
                before=before_json.get(key),
                after=after_json.get(key),
                sensitive=False,
            )
            for key in keys
            if before_json.get(key) != after_json.get(key)
        ]
        return ResourceDiff(
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            changes=changes,
        )

    async def list_mcp_server_references(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> ResourceReferencePage:
        access = await self._access(principal, "read", metadata)
        definition = await self._definition(access, _resource_id(resource_id))
        records, next_cursor = await self._reference_reader.list_references(
            access.context,
            target_type="mcp",
            target_id=definition.id,
            limit=200,
            cursor=None,
        )
        return ResourceReferencePage(
            items=[_reference(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("mcp", action):
            raise permission_denied()
        return access

    async def _definition(
        self, access: TenantAccess, definition_id: UUID
    ) -> ResourceDefinitionRecord:
        record = await self._registry.get_definition(
            access.context, resource_type="mcp", resource_id=definition_id
        )
        if record is None:
            raise resource_not_found()
        return record

    async def _definition_for_publication(
        self, access: TenantAccess, definition_id: UUID, expected: int
    ) -> ResourceDefinitionRecord:
        definition = await self._definition(access, definition_id)
        if definition.resource_version != expected:
            raise resource_version_conflict()
        return definition

    async def _replay(
        self,
        access: TenantAccess,
        *,
        operation_type: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyReplay | None:
        return await self._registry.get_idempotency_replay(
            access.context,
            actor_id=UUID(access.context.subject_id),
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )


def _validate(
    access: TenantAccess, content: ResourceContentMcp, *, publishing: bool = False
) -> None:
    try:
        validate_mcp_content(access.context, content, publishing=publishing)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _mcp_content(
    record: ResourceDefinitionRecord | ResourceVersionRecord,
) -> ResourceContentMcp:
    if not isinstance(record.content, ResourceContentMcp):
        raise resource_state_conflict("MCP content is unavailable.")
    return record.content


def _resource(record: ResourceDefinitionRecord) -> Resource:
    return Resource(
        id=str(record.id),
        resource_type=record.resource_type,
        code=record.code,
        name=record.name,
        description=record.description,
        visibility=record.visibility,
        content_schema_version=record.content_schema_version,
        content=record.content,
        status=record.status,
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _version(record: ResourceVersionRecord) -> ResourceVersion:
    return ResourceVersion(
        id=str(record.id),
        definition_id=str(record.definition_id),
        version_no=record.version_no,
        content_hash=record.content_hash,
        release_note=record.release_note,
        published_at=record.published_at,
    )


def _reference(record: ResourceReferenceRecord) -> ResourceReference:
    return ResourceReference(
        resource_type=record.resource_type,
        resource_id=str(record.resource_id),
        reference_type=record.reference_type,
        version_id=str(record.version_id) if record.version_id is not None else None,
    )


def _operation_outcome(
    outcome: MutationOutcome[OperationRecord],
) -> OperationAccepted:
    if outcome.replay is not None:
        return OperationAccepted.model_validate(outcome.replay.response_body)
    if outcome.value is None:
        raise RuntimeError("operation outcome is missing its value")
    return OperationAccepted(
        operation_id=str(outcome.value.id),
        status="ACCEPTED",
        status_url=f"/api/v1/operations/{outcome.value.id}",
    )


def _versioned_outcome[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        return _versioned_replay(outcome.replay, model_type)
    if outcome.value is None:
        raise RuntimeError("mutation outcome is missing its value")
    model = mapper(outcome.value)
    resource_version = getattr(outcome.value, "resource_version", None)
    if not isinstance(resource_version, int):
        raise TypeError("versioned outcome is missing resource_version")
    return model, format_etag(resource_version)


def _versioned_replay[ModelT: BaseModel](
    replay: IdempotencyReplay, model_type: type[ModelT]
) -> tuple[ModelT, str]:
    if replay.response_etag is None:
        raise RuntimeError("versioned replay is missing ETag")
    return model_type.model_validate(replay.response_body), replay.response_etag


def _outcome_value[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> ModelT:
    if outcome.replay is not None:
        return model_type.model_validate(outcome.replay.response_body)
    if outcome.value is None:
        raise RuntimeError("mutation outcome is missing its value")
    return mapper(outcome.value)


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise resource_not_found() from exc


def _expected_version(if_match: str) -> int:
    try:
        return parse_etag(if_match)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _request_hash(
    operation: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation, request, extra=extra)
