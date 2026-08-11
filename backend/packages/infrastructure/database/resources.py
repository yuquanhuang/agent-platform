"""Tenant-scoped persistence for the shared versioned resource registry."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import RequestMetadata
from packages.contracts.generated.resource_content import (
    ResourceContentMcp,
    ResourceContentModelConfig,
    ResourceContentModelProvider,
)
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ResourceCopyRequest,
    ResourceCreateRequest,
    ResourcePublishRequest,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
)
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.outbox import OutboxEvent, OutboxStatus
from packages.domain.public import (
    IdempotencyReplay,
    MutationOutcome,
    OperationRecord,
    OperationStatus,
    ResourceContentValue,
    ResourceDefinitionRecord,
    ResourceRegistryStatus,
    ResourceType,
    ResourceVersionRecord,
    ResourceVersionStatus,
    ResourceVisibility,
    canonical_content_hash,
    decode_cursor,
    encode_cursor,
    parse_resource_content,
    resource_content_json,
    validate_content_type,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
    get_idempotency_replay,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    McpCapabilityDiscoveryModel,
    ModelBindingSnapshotModel,
    OperationRecordModel,
    ResourceDefinitionModel,
    ResourceVersionModel,
    SkillSupplyChainScanModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyResourceRegistry:
    """Persist resource definitions and versions inside tenant transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_idempotency_replay(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        operation_type: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyReplay | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            return await get_idempotency_replay(
                unit_of_work.session,
                tenant_id=UUID(context.tenant_id),
                actor_id=actor_id,
                operation_type=operation_type,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )

    async def create_definition(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        request: ResourceCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ResourceDefinitionRecord]:
        content = _validated_content(resource_type, request.content)
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            existing = await session.scalar(
                select(ResourceDefinitionModel.id).where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.code == request.code,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            if existing is not None:
                raise resource_state_conflict("Resource code is already in use.")
            await _validate_resource_dependencies(
                session,
                tenant_id=tenant_id,
                resource_type=resource_type,
                content=content,
                require_provider_enabled=False,
            )
            now = datetime.now(UTC)
            model = ResourceDefinitionModel(
                tenant_id=tenant_id,
                resource_type=resource_type,
                code=request.code,
                name=request.name,
                description=request.description,
                owner_user_id=actor_id,
                visibility=request.visibility or "private",
                current_draft_json=_stored_content(content),
                draft_schema_version=request.content_schema_version,
                created_by=actor_id,
                updated_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            await session.flush()
            result = _definition_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.create",
                resource_type=resource_type,
                resource_id=model.id,
                metadata=metadata,
                change={"code": request.code, "resource_type": resource_type},
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_definition_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_definition(
        self,
        context: TenantContext,
        *,
        resource_type: ResourceType,
        resource_id: UUID,
    ) -> ResourceDefinitionRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(ResourceDefinitionModel).where(
                    ResourceDefinitionModel.tenant_id == UUID(context.tenant_id),
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            return _definition_record(model) if model is not None else None

    async def list_definitions(
        self,
        context: TenantContext,
        *,
        resource_type: ResourceType,
        limit: int,
        cursor: str | None,
        keyword: str | None = None,
    ) -> tuple[list[ResourceDefinitionRecord], str | None]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            tenant_id = UUID(context.tenant_id)
            statement = (
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .order_by(
                    ResourceDefinitionModel.created_at.desc(),
                    ResourceDefinitionModel.id.desc(),
                )
            )
            if keyword:
                statement = statement.where(
                    or_(
                        ResourceDefinitionModel.code.ilike(f"%{keyword}%"),
                        ResourceDefinitionModel.name.ilike(f"%{keyword}%"),
                    )
                )
            if cursor is not None:
                try:
                    created_at, resource_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error("Pagination cursor is invalid.") from exc
                statement = statement.where(
                    or_(
                        ResourceDefinitionModel.created_at < created_at,
                        and_(
                            ResourceDefinitionModel.created_at == created_at,
                            ResourceDefinitionModel.id < resource_id,
                        ),
                    )
                )
            rows = list((await session.scalars(statement.limit(limit + 1))).all())
            page = rows[:limit]
            next_cursor = (
                encode_cursor(page[-1].created_at, page[-1].id)
                if len(rows) > limit and page
                else None
            )
            return [_definition_record(row) for row in page], next_cursor

    async def update_definition(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        expected_version: int,
        request: ResourceUpdateRequest,
        metadata: RequestMetadata,
    ) -> ResourceDefinitionRecord | None:
        content = (
            _validated_content(resource_type, request.content)
            if request.content is not None
            else None
        )
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            model = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == UUID(context.tenant_id),
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            if model.resource_version != expected_version:
                raise resource_version_conflict()
            if content is not None:
                await _validate_resource_dependencies(
                    session,
                    tenant_id=model.tenant_id,
                    resource_type=resource_type,
                    content=content,
                    require_provider_enabled=False,
                )
            if request.name is not None:
                model.name = request.name
            if "description" in request.model_fields_set:
                model.description = request.description
            if request.visibility is not None:
                model.visibility = request.visibility
            if request.content_schema_version is not None:
                model.draft_schema_version = request.content_schema_version
            if content is not None:
                model.current_draft_json = _stored_content(content)
            model.resource_version += 1
            model.updated_by = actor_id
            model.updated_at = datetime.now(UTC)
            await session.flush()
            result = _definition_record(model)
            await _add_audit(
                session,
                tenant_id=model.tenant_id,
                actor_id=actor_id,
                action="resource.update",
                resource_type=resource_type,
                resource_id=model.id,
                metadata=metadata,
                change=_update_audit_summary(request, content),
            )
            return result

    async def publish_version(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        request: ResourcePublishRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        scan_attestation_id: UUID | None = None,
        mcp_discovery_attestation_id: UUID | None = None,
    ) -> MutationOutcome[ResourceVersionRecord]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.publish",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                raise resource_state_conflict("Resource definition is not available.")
            if definition.resource_version != request.expected_resource_version:
                raise resource_version_conflict()
            content = _validated_content(
                resource_type, parse_resource_content(definition.current_draft_json)
            )
            provider_dependency = await _validate_resource_dependencies(
                session,
                tenant_id=tenant_id,
                resource_type=resource_type,
                content=content,
                require_provider_enabled=True,
            )
            content_hash = canonical_content_hash(content)
            scan = await _require_skill_scan(
                session,
                tenant_id=tenant_id,
                definition_id=resource_id,
                draft_resource_version=definition.resource_version,
                content_hash=content_hash,
                resource_type=resource_type,
                scan_attestation_id=scan_attestation_id,
            )
            discovery = await _require_mcp_discovery(
                session,
                tenant_id=tenant_id,
                definition_id=resource_id,
                draft_resource_version=definition.resource_version,
                content_hash=content_hash,
                resource_type=resource_type,
                discovery_attestation_id=mcp_discovery_attestation_id,
                content=content,
            )
            duplicate = await session.scalar(
                select(ResourceVersionModel.id).where(
                    ResourceVersionModel.tenant_id == tenant_id,
                    ResourceVersionModel.definition_id == resource_id,
                    ResourceVersionModel.content_hash == content_hash,
                    ResourceVersionModel.publication_kind == "PUBLISH",
                )
            )
            if duplicate is not None:
                raise resource_state_conflict(
                    "The resource content is already published."
                )
            next_version = (
                await session.scalar(
                    select(
                        func.coalesce(func.max(ResourceVersionModel.version_no), 0)
                    ).where(
                        ResourceVersionModel.tenant_id == tenant_id,
                        ResourceVersionModel.definition_id == resource_id,
                    )
                )
                or 0
            ) + 1
            now = datetime.now(UTC)
            version_model = ResourceVersionModel(
                tenant_id=tenant_id,
                definition_id=resource_id,
                version_no=next_version,
                schema_version=definition.draft_schema_version,
                content_json=_stored_content(content),
                content_hash=content_hash,
                release_note=request.release_note,
                publication_kind="PUBLISH",
                published_by=actor_id,
                published_at=now,
            )
            session.add(version_model)
            definition.status = "ACTIVE"
            definition.resource_version += 1
            definition.updated_by = actor_id
            definition.updated_at = now
            await session.flush()
            if scan is not None:
                scan.published_version_id = version_model.id
                await session.flush()
            if discovery is not None:
                discovery.published_version_id = version_model.id
                await session.flush()
            await _create_model_binding_snapshot(
                session,
                tenant_id=tenant_id,
                model_config_content=content,
                version=version_model,
                provider_dependency=provider_dependency,
            )
            result = _version_record(version_model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.publish",
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata,
                change={
                    "version_no": next_version,
                    "content_hash": content_hash,
                    "scan_attestation_id": str(scan.id) if scan is not None else None,
                    "mcp_discovery_attestation_id": (
                        str(discovery.id) if discovery is not None else None
                    ),
                },
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_version_json(result),
                response_etag=None,
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def copy_definition(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        request: ResourceCopyRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ResourceDefinitionRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.copy",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            source = await session.scalar(
                select(ResourceDefinitionModel).where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            if source is None:
                return None
            duplicate = await session.scalar(
                select(ResourceDefinitionModel.id).where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.code == request.code,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            if duplicate is not None:
                raise resource_state_conflict("Resource code is already in use.")
            now = datetime.now(UTC)
            model = ResourceDefinitionModel(
                tenant_id=tenant_id,
                resource_type=resource_type,
                code=request.code,
                name=request.name,
                description=source.description,
                owner_user_id=actor_id,
                visibility=source.visibility,
                current_draft_json=source.current_draft_json,
                draft_schema_version=source.draft_schema_version,
                created_by=actor_id,
                updated_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            await session.flush()
            result = _definition_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.copy",
                resource_type=resource_type,
                resource_id=model.id,
                metadata=metadata,
                change={"source_resource_id": str(resource_id), "code": request.code},
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_definition_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def set_definition_status(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        expected_version: int,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ResourceDefinitionRecord] | None:
        tenant_id = UUID(context.tenant_id)
        operation = "enable" if enabled else "disable"
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.{operation}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                return None
            if definition.resource_version != expected_version:
                raise resource_version_conflict()
            if enabled:
                if definition.status != "DISABLED":
                    raise resource_state_conflict(
                        "Only a disabled resource can be enabled."
                    )
                has_version = await session.scalar(
                    select(ResourceVersionModel.id)
                    .where(
                        ResourceVersionModel.tenant_id == tenant_id,
                        ResourceVersionModel.definition_id == resource_id,
                    )
                    .limit(1)
                )
                definition.status = "ACTIVE" if has_version is not None else "DRAFT"
            else:
                if definition.status not in {"DRAFT", "ACTIVE"}:
                    raise resource_state_conflict(
                        "Resource cannot be disabled from its current state."
                    )
                definition.status = "DISABLED"
            definition.resource_version += 1
            definition.updated_by = actor_id
            definition.updated_at = datetime.now(UTC)
            await session.flush()
            result = _definition_record(definition)
            reason_digest = _optional_text_digest(request.reason if request else None)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=f"resource.{operation}",
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata,
                change={
                    "status": result.status,
                    **({"reason_digest": reason_digest} if reason_digest else {}),
                },
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=200,
                response_body=_definition_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def request_model_provider_connection_test(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_id: UUID,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord] | None:
        """Persist a deferred connection-test request without resolving its Secret."""

        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="model_provider.connection_test",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == "model_provider",
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                return None
            if definition.status == "DISABLED":
                raise resource_state_conflict(
                    "A disabled Model Provider cannot be connection-tested."
                )
            content = parse_resource_content(definition.current_draft_json)
            if not isinstance(content, ResourceContentModelProvider):
                raise resource_state_conflict(
                    "Model Provider content is not available for connection testing."
                )
            now = datetime.now(UTC)
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="model_provider.connection_test",
                status="ACCEPTED",
                resource_type="model_provider",
                resource_id=resource_id,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            await session.flush()
            event = OutboxEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                aggregate_type="model_provider",
                aggregate_id=resource_id,
                event_type="model_provider.connection_test_requested",
                payload={
                    "operation_id": str(operation.id),
                    "provider_id": str(resource_id),
                    "provider_type": content.provider_type,
                    "base_url": content.base_url,
                    "secret_ref": content.secret_ref,
                    "timeout_seconds": content.timeout_seconds,
                },
                payload_schema_version=1,
                status=OutboxStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
            SqlAlchemyOutboxWriter(session, context).add(event)
            result = _operation_record(operation)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="model_provider.connection_test.requested",
                resource_type="model_provider",
                resource_id=resource_id,
                metadata=metadata,
                change={"operation_id": str(operation.id)},
            )
            response_body: dict[str, object] = {
                "operation_id": str(operation.id),
                "status": "ACCEPTED",
                "status_url": f"/api/v1/operations/{operation.id}",
            }
            await complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body=response_body,
                response_etag=None,
                response_ref=str(operation.id),
            )
            return MutationOutcome(value=result)

    async def request_mcp_capability_discovery(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_id: UUID,
        expected_resource_version: int,
        content_hash: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord] | None:
        """Persist an MCP discovery request without contacting its endpoint."""

        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="mcp.discover",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == "mcp",
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                return None
            if definition.resource_version != expected_resource_version:
                raise resource_version_conflict()
            content = parse_resource_content(definition.current_draft_json)
            if not isinstance(content, ResourceContentMcp):
                raise resource_state_conflict(
                    "MCP content is unavailable for discovery."
                )
            if canonical_content_hash(content) != content_hash:
                raise resource_state_conflict("MCP content changed before discovery.")
            now = datetime.now(UTC)
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="mcp.discover",
                status="ACCEPTED",
                resource_type="mcp",
                resource_id=resource_id,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            await session.flush()
            SqlAlchemyOutboxWriter(session, context).add(
                OutboxEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="mcp",
                    aggregate_id=resource_id,
                    event_type="mcp.capability_discovery_requested",
                    payload={
                        "operation_id": str(operation.id),
                        "requested_by": str(actor_id),
                        "definition_id": str(resource_id),
                        "draft_resource_version": expected_resource_version,
                        "content_hash": content_hash,
                        "transport": content.transport,
                        "endpoint": content.endpoint,
                        "header_templates": content.header_templates or {},
                        "secret_refs": content.secret_refs,
                        "timeout_seconds": content.timeout_seconds,
                        "allowed_tools": content.allowed_tools or [],
                    },
                    payload_schema_version=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            result = _operation_record(operation)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="mcp.capability_discovery.requested",
                resource_type="mcp",
                resource_id=resource_id,
                metadata=metadata,
                change={
                    "operation_id": str(operation.id),
                    "content_hash": content_hash,
                },
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body={
                    "operation_id": str(operation.id),
                    "status": "ACCEPTED",
                    "status_url": f"/api/v1/operations/{operation.id}",
                },
                response_etag=None,
                response_ref=str(operation.id),
            )
            return MutationOutcome(value=result)

    async def delete_definition(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.delete",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                return None
            if definition.resource_version != expected_version:
                raise resource_version_conflict()
            if resource_type == "model_provider":
                reference = await session.scalar(
                    select(ResourceDefinitionModel.id)
                    .where(
                        ResourceDefinitionModel.tenant_id == tenant_id,
                        ResourceDefinitionModel.resource_type == "model_config",
                        ResourceDefinitionModel.deleted_at.is_(None),
                        ResourceDefinitionModel.current_draft_json[
                            "provider_id"
                        ].as_string()
                        == str(resource_id),
                    )
                    .limit(1)
                )
                if reference is not None:
                    raise resource_state_conflict(
                        "Model Provider is referenced by an active Model Config."
                    )
            now = datetime.now(UTC)
            definition.status = "DELETED"
            definition.deleted_at = now
            definition.deleted_by = actor_id
            definition.updated_at = now
            definition.updated_by = actor_id
            definition.resource_version += 1
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.delete",
                status="SUCCEEDED",
                resource_type=resource_type,
                resource_id=resource_id,
                result_json={"deleted": True},
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
            session.add(operation)
            await session.flush()
            result = _operation_record(operation)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.delete",
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata,
                change={"operation_id": str(result.id)},
            )
            response_body: dict[str, object] = {
                "operation_id": str(result.id),
                "status": "ACCEPTED",
                "status_url": f"/api/v1/operations/{result.id}",
            }
            await complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body=response_body,
                response_etag=None,
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def rollback_version(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID,
        source_version_id: UUID,
        request: ResourceRollbackRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        scan_attestation_id: UUID | None = None,
        mcp_discovery_attestation_id: UUID | None = None,
    ) -> MutationOutcome[ResourceVersionRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"{resource_type}.rollback",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            definition = await session.scalar(
                select(ResourceDefinitionModel)
                .where(
                    ResourceDefinitionModel.tenant_id == tenant_id,
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if definition is None:
                return None
            if definition.resource_version != request.expected_resource_version:
                raise resource_version_conflict()
            source = await session.scalar(
                select(ResourceVersionModel).where(
                    ResourceVersionModel.tenant_id == tenant_id,
                    ResourceVersionModel.definition_id == resource_id,
                    ResourceVersionModel.id == source_version_id,
                )
            )
            if source is None:
                return None
            source_content = _validated_content(
                resource_type, parse_resource_content(source.content_json)
            )
            scan = await _require_skill_scan(
                session,
                tenant_id=tenant_id,
                definition_id=resource_id,
                draft_resource_version=definition.resource_version,
                content_hash=source.content_hash,
                resource_type=resource_type,
                scan_attestation_id=scan_attestation_id,
            )
            discovery = await _require_mcp_discovery(
                session,
                tenant_id=tenant_id,
                definition_id=resource_id,
                draft_resource_version=definition.resource_version,
                content_hash=source.content_hash,
                resource_type=resource_type,
                discovery_attestation_id=mcp_discovery_attestation_id,
                content=source_content,
                source_version_id=source.id,
            )
            provider_dependency = await _validate_resource_dependencies(
                session,
                tenant_id=tenant_id,
                resource_type=resource_type,
                content=source_content,
                require_provider_enabled=True,
            )
            next_version = (
                await session.scalar(
                    select(
                        func.coalesce(func.max(ResourceVersionModel.version_no), 0)
                    ).where(
                        ResourceVersionModel.tenant_id == tenant_id,
                        ResourceVersionModel.definition_id == resource_id,
                    )
                )
                or 0
            ) + 1
            now = datetime.now(UTC)
            version = ResourceVersionModel(
                tenant_id=tenant_id,
                definition_id=resource_id,
                version_no=next_version,
                schema_version=source.schema_version,
                content_json=source.content_json,
                content_hash=source.content_hash,
                release_note=request.release_note,
                publication_kind="ROLLBACK",
                source_uri=f"resource-version:{source.id}",
                published_by=actor_id,
                published_at=now,
            )
            session.add(version)
            definition.current_draft_json = source.content_json
            definition.draft_schema_version = source.schema_version
            definition.status = "ACTIVE"
            definition.resource_version += 1
            definition.updated_by = actor_id
            definition.updated_at = now
            await session.flush()
            if scan is not None:
                scan.published_version_id = version.id
                await session.flush()
            if discovery is not None:
                discovery.published_version_id = version.id
                await session.flush()
            await _clone_or_create_model_binding_snapshot(
                session,
                tenant_id=tenant_id,
                model_config_content=source_content,
                source_version_id=source.id,
                version=version,
                provider_dependency=provider_dependency,
            )
            result = _version_record(version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.rollback",
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata,
                change={
                    "source_version_id": str(source.id),
                    "version_no": next_version,
                    "content_hash": source.content_hash,
                    "scan_attestation_id": str(scan.id) if scan is not None else None,
                    "mcp_discovery_attestation_id": (
                        str(discovery.id) if discovery is not None else None
                    ),
                },
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_version_json(result),
                response_etag=None,
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def list_versions(
        self,
        context: TenantContext,
        *,
        resource_type: ResourceType,
        resource_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[ResourceVersionRecord], str | None]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            definition_exists = await session.scalar(
                select(ResourceDefinitionModel.id).where(
                    ResourceDefinitionModel.tenant_id == UUID(context.tenant_id),
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            if definition_exists is None:
                return [], None
            statement = (
                select(ResourceVersionModel)
                .where(
                    ResourceVersionModel.tenant_id == UUID(context.tenant_id),
                    ResourceVersionModel.definition_id == resource_id,
                )
                .order_by(ResourceVersionModel.version_no.desc())
            )
            if cursor is not None:
                try:
                    _, version_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error("Pagination cursor is invalid.") from exc
                anchor = await session.scalar(
                    select(ResourceVersionModel.version_no).where(
                        ResourceVersionModel.id == version_id,
                        ResourceVersionModel.definition_id == resource_id,
                    )
                )
                if anchor is None:
                    raise validation_error("Pagination cursor is invalid.")
                statement = statement.where(ResourceVersionModel.version_no < anchor)
            rows = list((await session.scalars(statement.limit(limit + 1))).all())
            page = rows[:limit]
            next_cursor = (
                encode_cursor(page[-1].published_at, page[-1].id)
                if len(rows) > limit and page
                else None
            )
            return [_version_record(row) for row in page], next_cursor

    async def get_version(
        self,
        context: TenantContext,
        *,
        resource_type: ResourceType,
        resource_id: UUID,
        version_id: UUID,
    ) -> ResourceVersionRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            definition_exists = await session.scalar(
                select(ResourceDefinitionModel.id).where(
                    ResourceDefinitionModel.tenant_id == UUID(context.tenant_id),
                    ResourceDefinitionModel.resource_type == resource_type,
                    ResourceDefinitionModel.id == resource_id,
                    ResourceDefinitionModel.deleted_at.is_(None),
                )
            )
            if definition_exists is None:
                return None
            model = await session.scalar(
                select(ResourceVersionModel).where(
                    ResourceVersionModel.tenant_id == UUID(context.tenant_id),
                    ResourceVersionModel.definition_id == resource_id,
                    ResourceVersionModel.id == version_id,
                )
            )
            return _version_record(model) if model is not None else None


def _validated_content(
    resource_type: ResourceType, content: object
) -> ResourceContentValue:
    try:
        parsed = parse_resource_content(content)
        validate_content_type(resource_type, parsed)
        return parsed
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


async def _require_skill_scan(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    definition_id: UUID,
    draft_resource_version: int,
    content_hash: str,
    resource_type: ResourceType,
    scan_attestation_id: UUID | None,
) -> SkillSupplyChainScanModel | None:
    if resource_type != "skill":
        if scan_attestation_id is not None:
            raise resource_state_conflict(
                "Supply-chain scan evidence is only valid for Skill publication."
            )
        return None
    if scan_attestation_id is None:
        raise resource_state_conflict(
            "Skill publication requires passed supply-chain scan evidence."
        )
    scan = await session.scalar(
        select(SkillSupplyChainScanModel)
        .where(
            SkillSupplyChainScanModel.tenant_id == tenant_id,
            SkillSupplyChainScanModel.id == scan_attestation_id,
            SkillSupplyChainScanModel.definition_id == definition_id,
        )
        .with_for_update()
    )
    if (
        scan is None
        or scan.status != "PASSED"
        or scan.draft_resource_version != draft_resource_version
        or scan.content_hash != content_hash
        or scan.published_version_id is not None
    ):
        raise resource_state_conflict(
            "Skill scan evidence does not match the exact publication input."
        )
    return scan


async def _require_mcp_discovery(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    definition_id: UUID,
    draft_resource_version: int,
    content_hash: str,
    resource_type: ResourceType,
    discovery_attestation_id: UUID | None,
    content: ResourceContentValue,
    source_version_id: UUID | None = None,
) -> McpCapabilityDiscoveryModel | None:
    if resource_type != "mcp":
        if discovery_attestation_id is not None:
            raise resource_state_conflict(
                "MCP discovery evidence is only valid for MCP publication."
            )
        return None
    if discovery_attestation_id is None:
        raise resource_state_conflict(
            "MCP publication requires passed capability discovery evidence."
        )
    if not isinstance(content, ResourceContentMcp):
        raise resource_state_conflict("MCP content is unavailable for publication.")
    allowed_tools = tuple(content.allowed_tools or ())
    discovery = await session.scalar(
        select(McpCapabilityDiscoveryModel)
        .where(
            McpCapabilityDiscoveryModel.tenant_id == tenant_id,
            McpCapabilityDiscoveryModel.id == discovery_attestation_id,
            McpCapabilityDiscoveryModel.definition_id == definition_id,
        )
        .with_for_update()
    )
    if (
        discovery is None
        or discovery.status != "PASSED"
        or discovery.content_hash != content_hash
        or discovery.capability_hash is None
        or not _mcp_allowed_tools_match(discovery.tools_json, allowed_tools)
    ):
        raise resource_state_conflict(
            "MCP discovery evidence does not match the exact publication input."
        )
    if source_version_id is None:
        if (
            discovery.draft_resource_version != draft_resource_version
            or discovery.published_version_id is not None
        ):
            raise resource_state_conflict(
                "MCP discovery evidence does not match the exact publication input."
            )
        return discovery
    if discovery.published_version_id != source_version_id:
        raise resource_state_conflict(
            "MCP rollback requires discovery evidence bound to the source version."
        )
    clone = McpCapabilityDiscoveryModel(
        tenant_id=tenant_id,
        definition_id=definition_id,
        operation_id=None,
        draft_resource_version=draft_resource_version,
        content_hash=content_hash,
        status="PASSED",
        protocol_version=discovery.protocol_version,
        server_name=discovery.server_name,
        server_version=discovery.server_version,
        tools_json=list(discovery.tools_json),
        capability_hash=discovery.capability_hash,
        findings_json=list(discovery.findings_json),
        discovered_at=discovery.discovered_at,
        discovered_by=discovery.discovered_by,
        source_discovery_id=discovery.id,
    )
    session.add(clone)
    await session.flush()
    return clone


def _mcp_allowed_tools_match(
    tools_json: list[dict[str, object]], allowed_tools: tuple[str, ...]
) -> bool:
    discovered = {
        name for tool in tools_json if isinstance((name := tool.get("name")), str)
    }
    return len(discovered) == len(tools_json) and set(allowed_tools) <= discovered


async def _validate_resource_dependencies(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    resource_type: ResourceType,
    content: ResourceContentValue,
    require_provider_enabled: bool,
) -> tuple[ResourceDefinitionModel, ResourceContentModelProvider] | None:
    if resource_type != "model_config":
        return None
    if not isinstance(content, ResourceContentModelConfig):
        raise resource_state_conflict("Model Config content is not available.")
    try:
        provider_id = UUID(content.provider_id)
    except ValueError as exc:
        raise validation_error(
            "provider_id must be a valid resource identifier."
        ) from exc
    statement = select(ResourceDefinitionModel).where(
        ResourceDefinitionModel.tenant_id == tenant_id,
        ResourceDefinitionModel.resource_type == "model_provider",
        ResourceDefinitionModel.id == provider_id,
        ResourceDefinitionModel.deleted_at.is_(None),
    )
    if require_provider_enabled:
        statement = statement.with_for_update()
    provider = await session.scalar(statement)
    if provider is None:
        raise resource_state_conflict(
            "The referenced Model Provider is not available in this tenant."
        )
    if require_provider_enabled and provider.status == "DISABLED":
        raise resource_state_conflict(
            "A Model Config cannot be published with a disabled Model Provider."
        )
    provider_content = _validated_content(
        "model_provider", parse_resource_content(provider.current_draft_json)
    )
    if not isinstance(provider_content, ResourceContentModelProvider):
        raise resource_state_conflict("Model Provider content is not available.")
    return provider, provider_content


async def _create_model_binding_snapshot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    model_config_content: ResourceContentValue,
    version: ResourceVersionModel,
    provider_dependency: (
        tuple[ResourceDefinitionModel, ResourceContentModelProvider] | None
    ),
) -> None:
    if not isinstance(model_config_content, ResourceContentModelConfig):
        return
    if provider_dependency is None:
        raise resource_state_conflict("Model Provider content is not available.")
    provider, provider_content = provider_dependency
    payload = _model_binding_snapshot_payload(model_config_content, provider_content)
    session.add(
        ModelBindingSnapshotModel(
            tenant_id=tenant_id,
            model_config_definition_id=version.definition_id,
            model_config_version_id=version.id,
            provider_definition_id=provider.id,
            provider_type=provider_content.provider_type,
            base_url=provider_content.base_url,
            secret_ref=provider_content.secret_ref,
            provider_timeout_seconds=provider_content.timeout_seconds,
            model_id=model_config_content.model_id,
            capabilities_json=list(model_config_content.capabilities),
            default_parameters_json=dict(model_config_content.default_parameters),
            max_context_tokens=model_config_content.max_context_tokens,
            rate_limit_rpm=model_config_content.rate_limit_rpm,
            snapshot_hash=_snapshot_hash(payload),
            created_at=version.published_at,
        )
    )
    await session.flush()


async def _clone_or_create_model_binding_snapshot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    model_config_content: ResourceContentValue,
    source_version_id: UUID,
    version: ResourceVersionModel,
    provider_dependency: (
        tuple[ResourceDefinitionModel, ResourceContentModelProvider] | None
    ),
) -> None:
    if not isinstance(model_config_content, ResourceContentModelConfig):
        return
    source = await session.scalar(
        select(ModelBindingSnapshotModel).where(
            ModelBindingSnapshotModel.tenant_id == tenant_id,
            ModelBindingSnapshotModel.model_config_version_id == source_version_id,
        )
    )
    if source is None:
        await _create_model_binding_snapshot(
            session,
            tenant_id=tenant_id,
            model_config_content=model_config_content,
            version=version,
            provider_dependency=provider_dependency,
        )
        return
    session.add(
        ModelBindingSnapshotModel(
            tenant_id=tenant_id,
            model_config_definition_id=version.definition_id,
            model_config_version_id=version.id,
            provider_definition_id=source.provider_definition_id,
            provider_type=source.provider_type,
            base_url=source.base_url,
            secret_ref=source.secret_ref,
            provider_timeout_seconds=source.provider_timeout_seconds,
            model_id=source.model_id,
            capabilities_json=list(source.capabilities_json),
            default_parameters_json=dict(source.default_parameters_json),
            max_context_tokens=source.max_context_tokens,
            rate_limit_rpm=source.rate_limit_rpm,
            snapshot_hash=source.snapshot_hash,
            created_at=version.published_at,
        )
    )
    await session.flush()


def _model_binding_snapshot_payload(
    model_config: ResourceContentModelConfig,
    provider: ResourceContentModelProvider,
) -> dict[str, object]:
    return {
        "provider_id": model_config.provider_id,
        "provider_type": provider.provider_type,
        "base_url": provider.base_url,
        "secret_ref": provider.secret_ref,
        "provider_timeout_seconds": provider.timeout_seconds,
        "model_id": model_config.model_id,
        "capabilities": list(model_config.capabilities),
        "default_parameters": dict(model_config.default_parameters),
        "max_context_tokens": model_config.max_context_tokens,
        "rate_limit_rpm": model_config.rate_limit_rpm,
    }


def _snapshot_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _definition_record(model: ResourceDefinitionModel) -> ResourceDefinitionRecord:
    return ResourceDefinitionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        resource_type=cast(ResourceType, model.resource_type),
        code=model.code,
        name=model.name,
        description=model.description,
        owner_user_id=model.owner_user_id,
        visibility=cast(ResourceVisibility, model.visibility),
        content_schema_version=model.draft_schema_version,
        content=parse_resource_content(model.current_draft_json),
        status=cast(ResourceRegistryStatus, model.status),
        resource_version=model.resource_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _stored_content(content: ResourceContentValue) -> dict[str, object]:
    return cast(dict[str, object], resource_content_json(content))


def _version_record(model: ResourceVersionModel) -> ResourceVersionRecord:
    return ResourceVersionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        definition_id=model.definition_id,
        version_no=model.version_no,
        schema_version=model.schema_version,
        content=parse_resource_content(model.content_json),
        content_hash=model.content_hash,
        release_note=model.release_note,
        status=cast(ResourceVersionStatus, model.status),
        published_at=model.published_at,
        published_by=model.published_by,
    )


def _definition_json(record: ResourceDefinitionRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "resource_type": record.resource_type,
        "code": record.code,
        "name": record.name,
        "description": record.description,
        "visibility": record.visibility,
        "content_schema_version": record.content_schema_version,
        "content": resource_content_json(record.content),
        "status": record.status,
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _version_json(record: ResourceVersionRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "definition_id": str(record.definition_id),
        "version_no": record.version_no,
        "content_hash": record.content_hash,
        "release_note": record.release_note,
        "published_at": record.published_at.isoformat(),
    }


def _operation_record(model: OperationRecordModel) -> OperationRecord:
    return OperationRecord(
        id=model.id,
        operation_type=model.operation_type,
        status=cast(OperationStatus, model.status),
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        result=cast(dict[str, JsonValue] | None, model.result_json),
        error=cast(dict[str, JsonValue] | None, model.error_json),
        created_at=model.created_at,
        updated_at=model.updated_at,
        finished_at=model.finished_at,
    )


def _update_audit_summary(
    request: ResourceUpdateRequest, content: ResourceContentValue | None
) -> dict[str, object]:
    changed_fields = sorted(request.model_fields_set)
    summary: dict[str, object] = {"changed_fields": changed_fields}
    if content is not None:
        summary["content_hash"] = canonical_content_hash(content)
    return summary


def _optional_text_digest(value: str | None) -> str | None:
    if value is None:
        return None
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


async def _add_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID,
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )
