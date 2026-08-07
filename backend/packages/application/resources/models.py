"""Model Provider and Model Config governance use cases."""

from collections.abc import Callable
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, JsonValue

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.application.resources.references import CompositeResourceReferenceReader
from packages.contracts.generated.resource_content import (
    ResourceContentModelConfig,
    ResourceContentModelProvider,
)
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ModelConfigCreateRequest,
    ModelProviderCreateRequest,
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
    validation_error,
)
from packages.domain.public import (
    MutationOutcome,
    OperationRecord,
    ResourceDefinitionRecord,
    ResourceReferenceRecord,
    ResourceVersionRecord,
    TenantAccess,
    format_etag,
    parse_etag,
    resource_content_json,
)

SUPPORTED_PROVIDER_TYPES = frozenset({"openai", "qwen", "deepseek"})


class TenantAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class ModelRegistry(Protocol):
    async def list_definitions(
        self,
        context: TenantContext,
        *,
        resource_type: str,
        limit: int,
        cursor: str | None,
        keyword: str | None = None,
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

    async def request_model_provider_connection_test(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[OperationRecord] | None: ...


class ModelManagementService:
    """Authorize model governance and preserve Secret Reference boundaries."""

    def __init__(
        self,
        access_resolver: TenantAccessResolver,
        registry: ModelRegistry,
        reference_reader: CompositeResourceReferenceReader,
    ) -> None:
        self._access_resolver = access_resolver
        self._registry = registry
        self._reference_reader = reference_reader

    async def list_model_providers(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourcePage:
        return await self._list(
            principal, "model_provider", limit=limit, cursor=cursor, metadata=metadata
        )

    async def create_model_provider(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: ModelProviderCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        validate_model_provider_content(request.content)
        return await self._create(
            principal,
            "model_provider",
            request,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def get_model_provider(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        return await self._get(
            principal, "model_provider", resource_id=resource_id, metadata=metadata
        )

    async def update_model_provider(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if request.content is not None:
            if request.content.resource_type != "model_provider":
                raise validation_error(
                    "Model Provider content must use resource_type=model_provider."
                )
            validate_model_provider_content(request.content)
        return await self._update(
            principal,
            "model_provider",
            resource_id=resource_id,
            if_match=if_match,
            request=request,
            metadata=metadata,
        )

    async def delete_model_provider(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        return await self._delete(
            principal,
            "model_provider",
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def set_model_provider_enabled(
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
        return await self._set_enabled(
            principal,
            "model_provider",
            resource_id=resource_id,
            if_match=if_match,
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def test_model_provider_connection(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "model_provider", "execute", metadata)
        outcome = await self._registry.request_model_provider_connection_test(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_id=_resource_id(resource_id),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "model_provider.connection_test",
                None,
                extra={"resource_id": resource_id},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _operation_outcome(outcome)

    async def list_model_configs(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourcePage:
        return await self._list(
            principal, "model_config", limit=limit, cursor=cursor, metadata=metadata
        )

    async def create_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: ModelConfigCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        validate_model_config_content(request.content)
        return await self._create(
            principal,
            "model_config",
            request,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def get_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        return await self._get(
            principal, "model_config", resource_id=resource_id, metadata=metadata
        )

    async def update_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if request.content is not None:
            if request.content.resource_type != "model_config":
                raise validation_error(
                    "Model Config content must use resource_type=model_config."
                )
            validate_model_config_content(request.content)
        return await self._update(
            principal,
            "model_config",
            resource_id=resource_id,
            if_match=if_match,
            request=request,
            metadata=metadata,
        )

    async def delete_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        return await self._delete(
            principal,
            "model_config",
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def publish_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourcePublishRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "model_config", "publish", metadata)
        outcome = await self._registry.publish_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="model_config",
            resource_id=_resource_id(resource_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "model_config.publish", request, extra={"resource_id": resource_id}
            ),
            metadata=metadata,
        )
        return _value_outcome(outcome, ResourceVersion, _version)

    async def set_model_config_enabled(
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
        return await self._set_enabled(
            principal,
            "model_config",
            resource_id=resource_id,
            if_match=if_match,
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def rollback_model_config(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourceRollbackRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "model_config", "rollback", metadata)
        outcome = await self._registry.rollback_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="model_config",
            resource_id=_resource_id(resource_id),
            source_version_id=_resource_id(request.version_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "model_config.rollback", request, extra={"resource_id": resource_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _value_outcome(outcome, ResourceVersion, _version)

    async def list_model_config_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourceVersionPage:
        access = await self._access(principal, "model_config", "read", metadata)
        definition_id = _resource_id(resource_id)
        definition = await self._registry.get_definition(
            access.context,
            resource_type="model_config",
            resource_id=definition_id,
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._registry.list_versions(
            access.context,
            resource_type="model_config",
            resource_id=definition_id,
            limit=limit,
            cursor=cursor,
        )
        return ResourceVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def diff_model_config_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        from_version_id: str,
        to_version_id: str,
        metadata: RequestMetadata,
    ) -> ResourceDiff:
        access = await self._access(principal, "model_config", "read", metadata)
        definition_id = _resource_id(resource_id)
        before = await self._registry.get_version(
            access.context,
            resource_type="model_config",
            resource_id=definition_id,
            version_id=_resource_id(from_version_id),
        )
        after = await self._registry.get_version(
            access.context,
            resource_type="model_config",
            resource_id=definition_id,
            version_id=_resource_id(to_version_id),
        )
        if before is None or after is None:
            raise resource_not_found()
        return ResourceDiff(
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            changes=_model_config_diff(before, after),
        )

    async def list_model_config_references(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> ResourceReferencePage:
        access = await self._access(principal, "model_config", "read", metadata)
        definition_id = _resource_id(resource_id)
        definition = await self._registry.get_definition(
            access.context,
            resource_type="model_config",
            resource_id=definition_id,
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._reference_reader.list_references(
            access.context,
            target_type="model_config",
            target_id=definition_id,
            limit=200,
            cursor=None,
        )
        return ResourceReferencePage(
            items=[_reference(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def _list(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourcePage:
        access = await self._access(principal, resource_type, "list", metadata)
        records, next_cursor = await self._registry.list_definitions(
            access.context,
            resource_type=resource_type,
            limit=limit,
            cursor=cursor,
        )
        return ResourcePage(
            items=[_resource(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def _create(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        request: BaseModel,
        *,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, resource_type, "create", metadata)
        generic_request = ResourceCreateRequest.model_validate(
            request.model_dump(mode="json")
        )
        outcome = await self._registry.create_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type=resource_type,
            request=generic_request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(f"{resource_type}.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Resource, _resource)

    async def _get(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, resource_type, "read", metadata)
        record = await self._registry.get_definition(
            access.context,
            resource_type=resource_type,
            resource_id=_resource_id(resource_id),
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def _update(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if not request.model_fields_set:
            raise validation_error("Resource update requires at least one field.")
        access = await self._access(principal, resource_type, "update", metadata)
        record = await self._registry.update_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type=resource_type,
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def _delete(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        *,
        resource_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, resource_type, "delete", metadata)
        outcome = await self._registry.delete_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type=resource_type,
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"{resource_type}.delete",
                None,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _operation_outcome(outcome)

    async def _set_enabled(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        *,
        resource_id: str,
        if_match: str,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, resource_type, "disable", metadata)
        operation = "enable" if enabled else "disable"
        outcome = await self._registry.set_definition_status(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type=resource_type,
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"{resource_type}.{operation}",
                request,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        resource_type: str,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows(resource_type, action):
            raise permission_denied()
        return access


def validate_model_provider_content(content: ResourceContentModelProvider) -> None:
    if content.provider_type not in SUPPORTED_PROVIDER_TYPES:
        raise validation_error(
            "provider_type must be one of openai, qwen, or deepseek."
        )
    parsed = urlsplit(content.base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise validation_error("Base URL must be a safe HTTP(S) endpoint.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise validation_error("External Model Provider Base URLs must use HTTPS.")
    secret = urlsplit(content.secret_ref)
    segments = [segment for segment in secret.path.split("/") if segment]
    if (
        secret.scheme != "secret"
        or secret.netloc != "tenant"
        or len(segments) < 2
        or any(segment in {".", ".."} for segment in segments)
        or secret.query
        or secret.fragment
        or secret.username is not None
        or secret.password is not None
    ):
        raise validation_error("secret_ref must be a safe tenant Secret Reference.")


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_model_config_content(content: ResourceContentModelConfig) -> None:
    try:
        UUID(content.provider_id)
    except ValueError as exc:
        raise validation_error(
            "provider_id must be a valid resource identifier."
        ) from exc
    if not content.model_id.strip():
        raise validation_error("model_id must not be empty.")
    if len(content.capabilities) != len(set(content.capabilities)):
        raise validation_error("capabilities must be unique.")


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
    operation_type: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation_type, request, extra=extra)


def _operation_outcome(outcome: MutationOutcome[OperationRecord]) -> OperationAccepted:
    if outcome.replay is not None:
        return OperationAccepted.model_validate(outcome.replay.response_body)
    if outcome.value is None:
        raise RuntimeError("operation outcome is missing its value")
    return OperationAccepted(
        operation_id=str(outcome.value.id),
        status="ACCEPTED",
        status_url=f"/api/v1/operations/{outcome.value.id}",
    )


class VersionedRecord(Protocol):
    @property
    def resource_version(self) -> int: ...


def _versioned_outcome[RecordT: VersionedRecord, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        model = model_type.model_validate(outcome.replay.response_body)
        if outcome.replay.response_etag is None:
            raise RuntimeError("versioned idempotency replay is missing ETag")
        return model, outcome.replay.response_etag
    if outcome.value is None:
        raise RuntimeError("versioned outcome is missing its value")
    return mapper(outcome.value), format_etag(outcome.value.resource_version)


def _value_outcome[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> ModelT:
    if outcome.replay is not None:
        return model_type.model_validate(outcome.replay.response_body)
    if outcome.value is None:
        raise RuntimeError("outcome is missing its value")
    return mapper(outcome.value)


def _model_config_diff(
    before: ResourceVersionRecord, after: ResourceVersionRecord
) -> list[ResourceDiffChangesItem]:
    changes: list[ResourceDiffChangesItem] = []
    _diff_values(
        "",
        resource_content_json(before.content),
        resource_content_json(after.content),
        changes,
    )
    return changes


def _diff_values(
    path: str,
    before: JsonValue | None,
    after: JsonValue | None,
    changes: list[ResourceDiffChangesItem],
) -> None:
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _diff_values(f"{path}/{key}", before.get(key), after.get(key), changes)
        return
    changes.append(
        ResourceDiffChangesItem(
            path=path or "/",
            change_type=(
                "added" if before is None else "removed" if after is None else "changed"
            ),
            before=before,
            after=after,
            sensitive=False,
        )
    )
