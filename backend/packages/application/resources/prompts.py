"""Prompt use cases over the shared versioned resource registry."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, JsonValue

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.application.resources.references import CompositeResourceReferenceReader
from packages.contracts.generated.resources_models import (
    ActionRequest,
    OperationAccepted,
    PromptCreateRequest,
    Resource,
    ResourceCopyRequest,
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


class TenantAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class PromptRegistry(Protocol):
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

    async def copy_definition(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ResourceDefinitionRecord] | None: ...

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


class PromptManagementService:
    """Authorize Prompt operations and map records to the frozen API models."""

    def __init__(
        self,
        access_resolver: TenantAccessResolver,
        registry: PromptRegistry,
        reference_reader: CompositeResourceReferenceReader,
    ) -> None:
        self._access_resolver = access_resolver
        self._registry = registry
        self._reference_reader = reference_reader

    async def list_prompts(
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
            resource_type="prompt",
            limit=limit,
            cursor=cursor,
            keyword=keyword,
        )
        return ResourcePage(
            items=[_resource(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: PromptCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "create", metadata)
        generic_request = ResourceCreateRequest.model_validate(
            request.model_dump(mode="json")
        )
        outcome = await self._registry.create_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="prompt",
            request=generic_request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("prompt.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Resource, _resource)

    async def get_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._registry.get_definition(
            access.context,
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def update_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if not request.model_fields_set:
            raise validation_error("Prompt update requires at least one field.")
        if request.content is not None and request.content.resource_type != "prompt":
            raise validation_error("Prompt content must use resource_type=prompt.")
        access = await self._access(principal, "update", metadata)
        record = await self._registry.update_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def delete_prompt(
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
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "prompt.delete",
                None,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return OperationAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("delete outcome is missing its value")
        return OperationAccepted(
            operation_id=str(outcome.value.id),
            status="ACCEPTED",
            status_url=f"/api/v1/operations/{outcome.value.id}",
        )

    async def publish_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourcePublishRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "publish", metadata)
        outcome = await self._registry.publish_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "prompt.publish", request, extra={"resource_id": resource_id}
            ),
            metadata=metadata,
        )
        return _outcome_value(outcome, ResourceVersion, _version)

    async def copy_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourceCopyRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "create", metadata)
        outcome = await self._registry.copy_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "prompt.copy", request, extra={"resource_id": resource_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def set_prompt_enabled(
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
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"prompt.{operation}",
                request,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def rollback_prompt(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourceRollbackRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ResourceVersion:
        access = await self._access(principal, "rollback", metadata)
        outcome = await self._registry.rollback_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
            source_version_id=_resource_id(request.version_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "prompt.rollback", request, extra={"resource_id": resource_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _outcome_value(outcome, ResourceVersion, _version)

    async def list_prompt_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ResourceVersionPage:
        access = await self._access(principal, "read", metadata)
        definition = await self._registry.get_definition(
            access.context,
            resource_type="prompt",
            resource_id=_resource_id(resource_id),
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._registry.list_versions(
            access.context,
            resource_type="prompt",
            resource_id=definition.id,
            limit=limit,
            cursor=cursor,
        )
        return ResourceVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def diff_prompt_versions(
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
            resource_type="prompt",
            resource_id=definition_id,
            version_id=_resource_id(from_version_id),
        )
        after = await self._registry.get_version(
            access.context,
            resource_type="prompt",
            resource_id=definition_id,
            version_id=_resource_id(to_version_id),
        )
        if before is None or after is None:
            raise resource_not_found()
        return ResourceDiff(
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            changes=_prompt_diff(before, after),
        )

    async def list_prompt_references(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> ResourceReferencePage:
        access = await self._access(principal, "read", metadata)
        definition_id = _resource_id(resource_id)
        definition = await self._registry.get_definition(
            access.context,
            resource_type="prompt",
            resource_id=definition_id,
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._reference_reader.list_references(
            access.context,
            target_type="prompt",
            target_id=definition_id,
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
        if not access.allows("prompt", action):
            raise permission_denied()
        return access


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


def _versioned_outcome[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        model = model_type.model_validate(outcome.replay.response_body)
        if outcome.replay.response_etag is None:
            raise RuntimeError("versioned replay is missing ETag")
        return model, outcome.replay.response_etag
    if outcome.value is None:
        raise RuntimeError("mutation outcome is missing its value")
    model = mapper(outcome.value)
    resource_version = getattr(outcome.value, "resource_version", None)
    if not isinstance(resource_version, int):
        raise TypeError("versioned outcome is missing resource_version")
    return model, format_etag(resource_version)


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


def _expected_version(value: str) -> int:
    try:
        return parse_etag(value)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _request_hash(
    operation_type: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation_type, request, extra=extra)


def _prompt_diff(
    before: ResourceVersionRecord, after: ResourceVersionRecord
) -> list[ResourceDiffChangesItem]:
    before_json = resource_content_json(before.content)
    after_json = resource_content_json(after.content)
    sensitive_names = _sensitive_variable_names(
        before_json
    ) | _sensitive_variable_names(after_json)
    changes: list[ResourceDiffChangesItem] = []
    _diff_values("", before_json, after_json, sensitive_names, changes)
    return changes


def _sensitive_variable_names(content: dict[str, JsonValue]) -> set[str]:
    variables = content.get("variables")
    if not isinstance(variables, list):
        return set()
    return {
        str(variable.get("name"))
        for variable in variables
        if isinstance(variable, dict)
        and variable.get("sensitive") is True
        and isinstance(variable.get("name"), str)
    }


def _diff_values(
    path: str,
    before: JsonValue | None,
    after: JsonValue | None,
    sensitive_names: set[str],
    changes: list[ResourceDiffChangesItem],
) -> None:
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}/{key}"
            _diff_values(
                child_path,
                before.get(key),
                after.get(key),
                sensitive_names,
                changes,
            )
        return
    if isinstance(before, list) and isinstance(after, list) and path == "/variables":
        before_by_name = _variables_by_name(before)
        after_by_name = _variables_by_name(after)
        for name in sorted(set(before_by_name) | set(after_by_name)):
            _diff_values(
                f"{path}/{name}",
                before_by_name.get(name),
                after_by_name.get(name),
                sensitive_names,
                changes,
            )
        return
    sensitive = _is_sensitive_path(path, sensitive_names)
    changes.append(
        ResourceDiffChangesItem(
            path=path or "/",
            change_type=(
                "added" if before is None else "removed" if after is None else "changed"
            ),
            before="[REDACTED]" if sensitive and before is not None else before,
            after="[REDACTED]" if sensitive and after is not None else after,
            sensitive=sensitive,
        )
    )


def _variables_by_name(values: list[JsonValue]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for value in values:
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                result[name] = value
    return result


def _is_sensitive_path(path: str, sensitive_names: set[str]) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 4
        and parts[1] == "variables"
        and parts[2] in sensitive_names
        and parts[3] == "default"
    )
