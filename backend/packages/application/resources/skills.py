"""Skill import, versioning and supply-chain publication use cases."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, JsonValue

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.application.resources.references import CompositeResourceReferenceReader
from packages.application.resources.skill_package import (
    SkillArtifactReader,
    SkillScanStore,
    SkillSupplyChainScanner,
    validate_skill_package,
)
from packages.contracts.generated.resource_content import ResourceContentSkill
from packages.contracts.generated.resources_models import (
    ActionRequest,
    OperationAccepted,
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
    SkillCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    dependency_unavailable,
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
    SkillScanEvidenceRecord,
    TenantAccess,
    format_etag,
    parse_etag,
    resource_content_json,
)


class SkillAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class SkillRegistry(Protocol):
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


class SkillManagementService:
    """Authorize Skill operations and bind publication to scan evidence."""

    def __init__(
        self,
        access_resolver: SkillAccessResolver,
        registry: SkillRegistry,
        reference_reader: CompositeResourceReferenceReader,
        artifact_reader: SkillArtifactReader,
        scanner: SkillSupplyChainScanner,
        scan_store: SkillScanStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._registry = registry
        self._reference_reader = reference_reader
        self._artifact_reader = artifact_reader
        self._scanner = scanner
        self._scan_store = scan_store

    async def list_skills(
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
            resource_type="skill",
            limit=limit,
            cursor=cursor,
            keyword=keyword,
        )
        return ResourcePage(
            items=[_resource(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_skill(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: SkillCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "create", metadata)
        request_hash = _request_hash("skill.create", request)
        replay = await self._replay(
            access,
            operation_type="skill.create",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return _versioned_replay(replay, Resource)
        await self._validate_content(access, request.content)
        outcome = await self._registry.create_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="skill",
            request=ResourceCreateRequest.model_validate(
                request.model_dump(mode="json")
            ),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Resource, _resource)

    async def get_skill(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._registry.get_definition(
            access.context,
            resource_type="skill",
            resource_id=_resource_id(resource_id),
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def update_skill(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        if_match: str,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        if not request.model_fields_set:
            raise validation_error("Skill update requires at least one field.")
        if request.content is not None and request.content.resource_type != "skill":
            raise validation_error("Skill content must use resource_type=skill.")
        access = await self._access(principal, "update", metadata)
        if request.content is not None:
            await self._validate_content(access, request.content)
        record = await self._registry.update_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="skill",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _resource(record), format_etag(record.resource_version)

    async def delete_skill(
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
            resource_type="skill",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "skill.delete",
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

    async def publish_skill(
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
            "skill.publish", request, extra={"resource_id": resource_id}
        )
        replay = await self._replay(
            access,
            operation_type="skill.publish",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return ResourceVersion.model_validate(replay.response_body)
        definition_id = _resource_id(resource_id)
        definition = await self._definition_for_scan(
            access, definition_id, request.expected_resource_version
        )
        evidence = await self._scan(
            access,
            definition_id=definition_id,
            draft_resource_version=request.expected_resource_version,
            content=_skill_content(definition),
            metadata=metadata,
        )
        outcome = await self._registry.publish_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="skill",
            resource_id=definition_id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
            scan_attestation_id=evidence.id,
        )
        return _outcome_value(outcome, ResourceVersion, _version)

    async def copy_skill(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        request: ResourceCopyRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Resource, str]:
        access = await self._access(principal, "create", metadata)
        request_hash = _request_hash(
            "skill.copy", request, extra={"resource_id": resource_id}
        )
        replay = await self._replay(
            access,
            operation_type="skill.copy",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return _versioned_replay(replay, Resource)
        source = await self._registry.get_definition(
            access.context,
            resource_type="skill",
            resource_id=_resource_id(resource_id),
        )
        if source is None:
            raise resource_not_found()
        if source.owner_user_id != UUID(access.context.subject_id):
            raise resource_state_conflict(
                "Skill copy requires Artifact ownership by the current user."
            )
        await self._validate_content(access, _skill_content(source))
        outcome = await self._registry.copy_definition(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="skill",
            resource_id=source.id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def set_skill_enabled(
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
            resource_type="skill",
            resource_id=_resource_id(resource_id),
            expected_version=_expected_version(if_match),
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"skill.{operation}",
                request,
                extra={"resource_id": resource_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Resource, _resource)

    async def rollback_skill(
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
            "skill.rollback", request, extra={"resource_id": resource_id}
        )
        replay = await self._replay(
            access,
            operation_type="skill.rollback",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return ResourceVersion.model_validate(replay.response_body)
        definition_id = _resource_id(resource_id)
        await self._definition_for_scan(
            access, definition_id, request.expected_resource_version
        )
        source = await self._registry.get_version(
            access.context,
            resource_type="skill",
            resource_id=definition_id,
            version_id=_resource_id(request.version_id),
        )
        if source is None:
            raise resource_not_found()
        evidence = await self._scan(
            access,
            definition_id=definition_id,
            draft_resource_version=request.expected_resource_version,
            content=_skill_content(source),
            metadata=metadata,
        )
        outcome = await self._registry.rollback_version(
            access.context,
            actor_id=UUID(access.context.subject_id),
            resource_type="skill",
            resource_id=definition_id,
            source_version_id=source.id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
            scan_attestation_id=evidence.id,
        )
        if outcome is None:
            raise resource_not_found()
        return _outcome_value(outcome, ResourceVersion, _version)

    async def list_skill_versions(
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
            resource_type="skill",
            resource_id=_resource_id(resource_id),
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._registry.list_versions(
            access.context,
            resource_type="skill",
            resource_id=definition.id,
            limit=limit,
            cursor=cursor,
        )
        return ResourceVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def diff_skill_versions(
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
            resource_type="skill",
            resource_id=definition_id,
            version_id=_resource_id(from_version_id),
        )
        after = await self._registry.get_version(
            access.context,
            resource_type="skill",
            resource_id=definition_id,
            version_id=_resource_id(to_version_id),
        )
        if before is None or after is None:
            raise resource_not_found()
        changes: list[ResourceDiffChangesItem] = []
        _diff_values(
            "",
            resource_content_json(before.content),
            resource_content_json(after.content),
            changes,
        )
        return ResourceDiff(
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            changes=changes,
        )

    async def list_skill_references(
        self,
        principal: AuthenticatedPrincipal,
        *,
        resource_id: str,
        metadata: RequestMetadata,
    ) -> ResourceReferencePage:
        access = await self._access(principal, "read", metadata)
        definition_id = _resource_id(resource_id)
        definition = await self._registry.get_definition(
            access.context, resource_type="skill", resource_id=definition_id
        )
        if definition is None:
            raise resource_not_found()
        records, next_cursor = await self._reference_reader.list_references(
            access.context,
            target_type="skill",
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
        if not access.allows("skill", action):
            raise permission_denied()
        return access

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

    async def _definition_for_scan(
        self,
        access: TenantAccess,
        definition_id: UUID,
        expected_resource_version: int,
    ) -> ResourceDefinitionRecord:
        definition = await self._registry.get_definition(
            access.context, resource_type="skill", resource_id=definition_id
        )
        if definition is None:
            raise resource_not_found()
        if definition.resource_version != expected_resource_version:
            raise resource_version_conflict()
        return definition

    async def _validate_content(
        self, access: TenantAccess, content: ResourceContentSkill
    ) -> None:
        try:
            await validate_skill_package(
                access.context,
                owner_user_id=UUID(access.context.subject_id),
                content=content,
                artifact_reader=self._artifact_reader,
            )
        except ValueError as exc:
            raise validation_error(str(exc)) from exc

    async def _scan(
        self,
        access: TenantAccess,
        *,
        definition_id: UUID,
        draft_resource_version: int,
        content: ResourceContentSkill,
        metadata: RequestMetadata,
    ) -> SkillScanEvidenceRecord:
        try:
            package = await validate_skill_package(
                access.context,
                owner_user_id=UUID(access.context.subject_id),
                content=content,
                artifact_reader=self._artifact_reader,
            )
        except ValueError as exc:
            raise validation_error(str(exc)) from exc
        result = await self._scanner.scan(access.context, package)
        evidence = await self._scan_store.record_scan(
            access.context,
            definition_id=definition_id,
            draft_resource_version=draft_resource_version,
            content_hash=package.content_hash,
            result=result,
            scanned_by=UUID(access.context.subject_id),
            metadata=metadata,
        )
        if result.status == "FAILED":
            raise dependency_unavailable("Skill supply-chain scanner failed.")
        if result.status == "REJECTED":
            raise resource_state_conflict(
                "Skill supply-chain scan rejected publication."
            )
        return evidence


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


def _skill_content(
    record: ResourceDefinitionRecord | ResourceVersionRecord,
) -> ResourceContentSkill:
    if not isinstance(record.content, ResourceContentSkill):
        raise resource_state_conflict("Stored Skill content is invalid.")
    return record.content


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
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            _diff_values(
                f"{path}/{index}",
                before[index] if index < len(before) else None,
                after[index] if index < len(after) else None,
                changes,
            )
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
