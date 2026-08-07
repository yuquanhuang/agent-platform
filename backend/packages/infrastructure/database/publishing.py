"""PostgreSQL Snapshot compilation and immutable reference adapters."""

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import String, and_, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import RequestMetadata
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    AgentRuntimeType,
    AgentSnapshotRecord,
    AgentVersionRecord,
    AgentVersionSnapshotRecord,
    AgentVisibility,
    BindingRole,
    PublishPreviewRecord,
    PublishPreviewTargetRecord,
    ResolvedPreviewBinding,
    ResolvedSnapshotBinding,
    ResourceReferenceRecord,
    ResourceType,
    SnapshotChangeRecord,
    SnapshotCompilationInput,
    SnapshotPublicationRecord,
    compile_agent_snapshot,
    decode_cursor,
    diff_agent_snapshot_content,
    encode_cursor,
)
from packages.domain.publishing.model import SnapshotBindingType
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentBindingModel,
    AgentDefinitionModel,
    AgentSnapshotModel,
    AgentVersionModel,
    AuditLogModel,
    DeploymentModel,
    ModelBindingSnapshotModel,
    ResourceDefinitionModel,
    ResourceVersionModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

RESOURCE_TYPE_MAP: dict[str, ResourceType] = {
    "prompt": "prompt",
    "skill": "skill",
    "mcp": "mcp",
    "model": "model_config",
    "sandbox": "sandbox_profile",
}
REFERENCE_TARGET_MAP: dict[ResourceType, str] = {
    value: key for key, value in RESOURCE_TYPE_MAP.items()
}


class SqlAlchemySnapshotCompilationStore:
    """Resolve and persist one Snapshot inside a repeatable-read transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_snapshot(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> AgentSnapshotRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(AgentSnapshotModel).where(
                    AgentSnapshotModel.tenant_id == tenant_id,
                    AgentSnapshotModel.id == snapshot_id,
                )
            )
            return _snapshot_record(model) if model is not None else None

    async def get_version(
        self, context: TenantContext, *, agent_version_id: UUID
    ) -> AgentVersionRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(AgentVersionModel).where(
                    AgentVersionModel.tenant_id == tenant_id,
                    AgentVersionModel.id == agent_version_id,
                )
            )
            return _version_record(model) if model is not None else None

    async def list_agent_versions(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[AgentVersionSnapshotRecord], str | None] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            session = unit_of_work.session
            exists = await session.scalar(
                select(AgentDefinitionModel.id).where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
            )
            if exists is None:
                return None
            statement = (
                select(AgentVersionModel, AgentSnapshotModel)
                .join(
                    AgentSnapshotModel,
                    and_(
                        AgentSnapshotModel.tenant_id == AgentVersionModel.tenant_id,
                        AgentSnapshotModel.agent_version_id == AgentVersionModel.id,
                    ),
                )
                .where(
                    AgentVersionModel.tenant_id == tenant_id,
                    AgentVersionModel.agent_id == agent_id,
                )
                .order_by(AgentVersionModel.version_no.desc())
            )
            if cursor is not None:
                try:
                    _, cursor_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error(
                        "Agent version pagination cursor is invalid."
                    ) from exc
                anchor = await session.scalar(
                    select(AgentVersionModel.version_no).where(
                        AgentVersionModel.tenant_id == tenant_id,
                        AgentVersionModel.agent_id == agent_id,
                        AgentVersionModel.id == cursor_id,
                    )
                )
                if anchor is None:
                    raise validation_error(
                        "Agent version pagination cursor is invalid."
                    )
                statement = statement.where(AgentVersionModel.version_no < anchor)
            rows = list((await session.execute(statement.limit(limit + 1))).all())
            page = rows[:limit]
            next_cursor = (
                encode_cursor(page[-1][0].created_at, page[-1][0].id)
                if len(rows) > limit and page
                else None
            )
            return (
                [
                    AgentVersionSnapshotRecord(
                        version=_version_record(version),
                        snapshot=_snapshot_record(snapshot),
                    )
                    for version, snapshot in page
                ],
                next_cursor,
            )

    async def get_agent_version(
        self, context: TenantContext, *, agent_id: UUID, version_id: UUID
    ) -> AgentVersionSnapshotRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            row = (
                await unit_of_work.session.execute(
                    select(AgentVersionModel, AgentSnapshotModel)
                    .join(
                        AgentSnapshotModel,
                        and_(
                            AgentSnapshotModel.tenant_id == AgentVersionModel.tenant_id,
                            AgentSnapshotModel.agent_version_id == AgentVersionModel.id,
                        ),
                    )
                    .where(
                        AgentVersionModel.tenant_id == tenant_id,
                        AgentVersionModel.agent_id == agent_id,
                        AgentVersionModel.id == version_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            version, snapshot = row
            return AgentVersionSnapshotRecord(
                version=_version_record(version),
                snapshot=_snapshot_record(snapshot),
            )

    async def diff_agent_snapshots(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        from_snapshot_id: UUID,
        to_snapshot_id: UUID,
    ) -> tuple[SnapshotChangeRecord, ...] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory,
            context,
            isolation_level="REPEATABLE READ",
            read_only=True,
        ) as unit_of_work:
            rows = list(
                (
                    await unit_of_work.session.execute(
                        select(AgentSnapshotModel, AgentVersionModel)
                        .join(
                            AgentVersionModel,
                            and_(
                                AgentVersionModel.tenant_id
                                == AgentSnapshotModel.tenant_id,
                                AgentVersionModel.id
                                == AgentSnapshotModel.agent_version_id,
                            ),
                        )
                        .where(
                            AgentSnapshotModel.tenant_id == tenant_id,
                            AgentVersionModel.agent_id == agent_id,
                            AgentSnapshotModel.id.in_(
                                {from_snapshot_id, to_snapshot_id}
                            ),
                        )
                    )
                ).all()
            )
            snapshots = {snapshot.id: snapshot for snapshot, _ in rows}
            before = snapshots.get(from_snapshot_id)
            after = snapshots.get(to_snapshot_id)
            if before is None or after is None:
                return None
            return diff_agent_snapshot_content(
                cast(dict[str, JsonValue], before.content_json),
                cast(dict[str, JsonValue], after.content_json),
            )

    async def preview_agent_publish(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        expected_agent_version: int,
        runtime_targets: tuple[str, ...],
    ) -> PublishPreviewRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory,
            context,
            isolation_level="REPEATABLE READ",
            read_only=True,
        ) as unit_of_work:
            session = unit_of_work.session
            agent = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
            )
            if agent is None:
                return None
            if agent.resource_version != expected_agent_version:
                raise resource_version_conflict()
            if agent.status not in {"DRAFT", "ACTIVE"}:
                raise resource_state_conflict(
                    "Agent cannot be previewed from its current state."
                )
            binding_models = list(
                (
                    await session.scalars(
                        select(AgentBindingModel).where(
                            AgentBindingModel.tenant_id == tenant_id,
                            AgentBindingModel.agent_id == agent_id,
                        )
                    )
                ).all()
            )
            bindings = await _resolve_bindings(
                session,
                tenant_id=tenant_id,
                root_agent_id=agent_id,
                binding_models=binding_models,
            )
            compiled = compile_agent_snapshot(
                SnapshotCompilationInput(
                    agent_id=agent.id,
                    code=agent.code,
                    name=agent.name,
                    description=agent.description,
                    runtime_type=cast(AgentRuntimeType, agent.runtime_type),
                    visibility=cast(AgentVisibility, agent.visibility),
                    tags=tuple(agent.tags_json),
                    default_language=agent.default_language,
                    draft_resource_version=agent.resource_version,
                    bindings=tuple(bindings),
                )
            )
            deployments = list(
                (
                    await session.scalars(
                        select(DeploymentModel).where(
                            DeploymentModel.tenant_id == tenant_id,
                            DeploymentModel.agent_id == agent_id,
                            DeploymentModel.runtime_target_id.in_(runtime_targets),
                            DeploymentModel.status == "ACTIVE",
                        )
                    )
                ).all()
            )
            deployments_by_target = {
                deployment.runtime_target_id: deployment for deployment in deployments
            }
            snapshot_ids = {deployment.snapshot_id for deployment in deployments}
            snapshots = (
                list(
                    (
                        await session.scalars(
                            select(AgentSnapshotModel).where(
                                AgentSnapshotModel.tenant_id == tenant_id,
                                AgentSnapshotModel.id.in_(snapshot_ids),
                            )
                        )
                    ).all()
                )
                if snapshot_ids
                else []
            )
            snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshots}
            targets: list[PublishPreviewTargetRecord] = []
            for runtime_target in runtime_targets:
                deployment = deployments_by_target.get(runtime_target)
                current_snapshot = (
                    snapshots_by_id.get(deployment.snapshot_id)
                    if deployment is not None
                    else None
                )
                if deployment is not None and current_snapshot is None:
                    raise resource_state_conflict(
                        "An ACTIVE Deployment references an unavailable Snapshot."
                    )
                targets.append(
                    PublishPreviewTargetRecord(
                        runtime_target_id=runtime_target,
                        current_deployment_id=(
                            deployment.id if deployment is not None else None
                        ),
                        current_snapshot_id=(
                            current_snapshot.id
                            if current_snapshot is not None
                            else None
                        ),
                        changes=diff_agent_snapshot_content(
                            (
                                cast(
                                    dict[str, JsonValue],
                                    current_snapshot.content_json,
                                )
                                if current_snapshot is not None
                                else None
                            ),
                            compiled.content,
                        ),
                    )
                )
            return PublishPreviewRecord(
                agent_id=agent_id,
                expected_agent_version=expected_agent_version,
                preview_snapshot_hash=compiled.content_hash,
                resolved_bindings=tuple(
                    ResolvedPreviewBinding(
                        resource_type=binding.resource_type,
                        resource_id=binding.resource_id,
                        version_id=binding.version_id,
                        version_no=binding.version_no,
                        content_hash=binding.content_hash,
                        binding_role=binding.binding_role,
                    )
                    for binding in sorted(bindings, key=_binding_preview_key)
                ),
                targets=tuple(targets),
                ready_to_publish=_snapshot_prerequisites_ready(
                    runtime_type=cast(AgentRuntimeType, agent.runtime_type),
                    bindings=bindings,
                ),
            )

    async def compile_snapshot(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_draft_resource_version: int,
        release_note: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> SnapshotPublicationRecord | None:
        for attempt in range(3):
            try:
                return await self._compile_snapshot_once(
                    context,
                    actor_id=actor_id,
                    agent_id=agent_id,
                    expected_draft_resource_version=expected_draft_resource_version,
                    release_note=release_note,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    metadata=metadata,
                )
            except DBAPIError as exc:
                if not _is_serialization_failure(exc):
                    raise
                if attempt == 2:
                    raise resource_version_conflict() from exc
                await asyncio.sleep(0.01 * (2**attempt))
        raise AssertionError("bounded Snapshot transaction retry did not terminate")

    async def _compile_snapshot_once(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_draft_resource_version: int,
        release_note: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> SnapshotPublicationRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory,
            context,
            isolation_level="REPEATABLE READ",
        ) as unit_of_work:
            session = unit_of_work.session
            idempotency_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.snapshot.compile",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return await _load_replayed_publication(
                    session, tenant_id=tenant_id, response=replay.response_body
                )

            agent = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if agent is None:
                return None
            if agent.resource_version != expected_draft_resource_version:
                raise resource_version_conflict()
            if agent.status not in {"DRAFT", "ACTIVE"}:
                raise resource_state_conflict(
                    "Agent cannot be published from its current state."
                )

            binding_models = list(
                (
                    await session.scalars(
                        select(AgentBindingModel).where(
                            AgentBindingModel.tenant_id == tenant_id,
                            AgentBindingModel.agent_id == agent_id,
                        )
                    )
                ).all()
            )
            bindings = await _resolve_bindings(
                session,
                tenant_id=tenant_id,
                root_agent_id=agent_id,
                binding_models=binding_models,
            )
            compiled = compile_agent_snapshot(
                SnapshotCompilationInput(
                    agent_id=agent.id,
                    code=agent.code,
                    name=agent.name,
                    description=agent.description,
                    runtime_type=cast(AgentRuntimeType, agent.runtime_type),
                    visibility=cast(AgentVisibility, agent.visibility),
                    tags=tuple(agent.tags_json),
                    default_language=agent.default_language,
                    draft_resource_version=agent.resource_version,
                    bindings=tuple(bindings),
                )
            )
            previous_version = await session.scalar(
                select(AgentVersionModel)
                .where(
                    AgentVersionModel.tenant_id == tenant_id,
                    AgentVersionModel.agent_id == agent_id,
                )
                .order_by(AgentVersionModel.version_no.desc())
                .limit(1)
            )
            next_version_no = (
                previous_version.version_no + 1 if previous_version is not None else 1
            )
            now = datetime.now(UTC)
            version_model = AgentVersionModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                version_no=next_version_no,
                created_from_version_id=(
                    previous_version.id if previous_version is not None else None
                ),
                release_note=release_note,
                created_at=now,
                created_by=actor_id,
            )
            session.add(version_model)
            await session.flush()
            snapshot_model = AgentSnapshotModel(
                tenant_id=tenant_id,
                agent_version_id=version_model.id,
                schema_version=compiled.schema_version,
                content_json=cast(dict[str, object], compiled.content),
                content_hash=compiled.content_hash,
                compiler_input_hash=compiled.compiler_input_hash,
                created_at=now,
                created_by=actor_id,
            )
            session.add(snapshot_model)
            agent.status = "ACTIVE"
            agent.resource_version += 1
            agent.updated_at = now
            agent.updated_by = actor_id
            await session.flush()

            publication = SnapshotPublicationRecord(
                version=_version_record(version_model),
                snapshot=_snapshot_record(snapshot_model),
            )
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                agent_id=agent_id,
                metadata=metadata,
                change={
                    "agent_version_id": str(version_model.id),
                    "version_no": next_version_no,
                    "snapshot_id": str(snapshot_model.id),
                    "content_hash": compiled.content_hash,
                    "compiler_input_hash": compiled.compiler_input_hash,
                    "binding_count": len(bindings),
                },
            )
            await complete_idempotency(
                session,
                idempotency_id,
                response_status=201,
                response_body={
                    "agent_version_id": str(version_model.id),
                    "snapshot_id": str(snapshot_model.id),
                },
                response_etag=f'"rv:{agent.resource_version}"',
                response_ref=str(snapshot_model.id),
            )
            return publication


class SqlAlchemyAgentResourceReferenceProvider:
    """Expose Agent Draft and Snapshot facts through the shared reference port."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_references(
        self,
        context: TenantContext,
        *,
        target_type: ResourceType,
        target_id: UUID,
        limit: int,
        after: ResourceReferenceRecord | None,
    ) -> Sequence[ResourceReferenceRecord]:
        binding_type = REFERENCE_TARGET_MAP.get(target_type)
        if binding_type is None:
            return ()
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            rows: set[ResourceReferenceRecord] = set()
            if _prefix_is_after(after, 0, "agent"):
                version_order = func.coalesce(
                    sql_cast(AgentBindingModel.fixed_version_id, String), ""
                )
                statement = (
                    select(
                        AgentBindingModel.agent_id,
                        AgentBindingModel.fixed_version_id,
                    )
                    .join(
                        AgentDefinitionModel,
                        and_(
                            AgentDefinitionModel.tenant_id
                            == AgentBindingModel.tenant_id,
                            AgentDefinitionModel.id == AgentBindingModel.agent_id,
                        ),
                    )
                    .where(
                        AgentBindingModel.tenant_id == tenant_id,
                        AgentBindingModel.resource_type == binding_type,
                        AgentBindingModel.resource_id == target_id,
                        AgentDefinitionModel.deleted_at.is_(None),
                    )
                    .order_by(
                        AgentBindingModel.agent_id,
                        version_order,
                    )
                    .limit(limit + 1)
                )
                if _same_prefix(after, 0, "agent"):
                    assert after is not None
                    statement = statement.where(
                        or_(
                            AgentBindingModel.agent_id > after.resource_id,
                            and_(
                                AgentBindingModel.agent_id == after.resource_id,
                                version_order > str(after.version_id or ""),
                            ),
                        )
                    )
                for agent_ref, version_ref in (await session.execute(statement)).all():
                    rows.add(
                        ResourceReferenceRecord(
                            resource_type="agent",
                            resource_id=agent_ref,
                            reference_type="draft_binding",
                            version_id=version_ref,
                        )
                    )

            if _prefix_is_after(after, 1, "snapshot"):
                snapshot_statement = (
                    select(AgentSnapshotModel)
                    .where(
                        AgentSnapshotModel.tenant_id == tenant_id,
                        AgentSnapshotModel.content_json.contains(
                            {
                                "bindings": [
                                    {
                                        "resource_type": binding_type,
                                        "resource_id": str(target_id),
                                    }
                                ]
                            }
                        ),
                    )
                    .order_by(AgentSnapshotModel.id)
                    .limit(limit + 1)
                )
                if _same_prefix(after, 1, "snapshot"):
                    assert after is not None
                    snapshot_statement = snapshot_statement.where(
                        AgentSnapshotModel.id >= after.resource_id
                    )
                snapshots = list((await session.scalars(snapshot_statement)).all())
                for snapshot in snapshots:
                    for binding in _snapshot_bindings(snapshot.content_json):
                        if binding.get("resource_type") == binding_type and binding.get(
                            "resource_id"
                        ) == str(target_id):
                            version_id = binding.get("version_id")
                            if isinstance(version_id, str):
                                rows.add(
                                    ResourceReferenceRecord(
                                        resource_type="snapshot",
                                        resource_id=snapshot.id,
                                        reference_type="snapshot",
                                        version_id=UUID(version_id),
                                    )
                                )
            ordered = sorted(rows, key=_reference_key)
            if after is not None:
                ordered = [
                    row
                    for row in ordered
                    if _reference_key(row) > _reference_key(after)
                ]
            return ordered[:limit]


async def _resolve_bindings(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    root_agent_id: UUID,
    binding_models: list[AgentBindingModel],
) -> list[ResolvedSnapshotBinding]:
    resolved: list[ResolvedSnapshotBinding] = []
    child_snapshots: list[AgentSnapshotModel] = []
    for binding in binding_models:
        if binding.resource_type == "agent":
            child_binding, child_snapshot = await _resolve_child_agent(
                session,
                tenant_id=tenant_id,
                root_agent_id=root_agent_id,
                binding=binding,
            )
            resolved.append(child_binding)
            child_snapshots.append(child_snapshot)
            continue
        expected_type = RESOURCE_TYPE_MAP.get(binding.resource_type)
        if expected_type is None:
            raise resource_state_conflict(
                f"Agent binding type {binding.resource_type!r} is not publishable."
            )
        definition = await session.scalar(
            select(ResourceDefinitionModel).where(
                ResourceDefinitionModel.tenant_id == tenant_id,
                ResourceDefinitionModel.id == binding.resource_id,
                ResourceDefinitionModel.resource_type == expected_type,
                ResourceDefinitionModel.status == "ACTIVE",
                ResourceDefinitionModel.deleted_at.is_(None),
            )
        )
        if definition is None:
            raise resource_state_conflict(
                "A bound resource is not enabled and publishable in this tenant."
            )
        version_statement = select(ResourceVersionModel).where(
            ResourceVersionModel.tenant_id == tenant_id,
            ResourceVersionModel.definition_id == definition.id,
            ResourceVersionModel.status == "PUBLISHED",
        )
        if binding.version_policy == "fixed":
            version_statement = version_statement.where(
                ResourceVersionModel.id == binding.fixed_version_id
            )
        else:
            version_statement = version_statement.order_by(
                ResourceVersionModel.version_no.desc()
            ).limit(1)
        version = await session.scalar(version_statement)
        if version is None:
            raise resource_state_conflict(
                "A bound resource has no eligible immutable version."
            )
        model_snapshot: ModelBindingSnapshotModel | None = None
        if binding.resource_type == "model":
            if binding.binding_role not in {
                "primary",
                "fallback_1",
                "fallback_2",
            }:
                raise resource_state_conflict(
                    "A Model Config binding is missing its routing role."
                )
            model_snapshot = await session.scalar(
                select(ModelBindingSnapshotModel).where(
                    ModelBindingSnapshotModel.tenant_id == tenant_id,
                    ModelBindingSnapshotModel.model_config_version_id == version.id,
                )
            )
            if model_snapshot is None:
                raise resource_state_conflict(
                    "A Model Config version is missing its immutable binding snapshot."
                )
        resolved.append(
            ResolvedSnapshotBinding(
                resource_type=cast(SnapshotBindingType, binding.resource_type),
                resource_id=definition.id,
                version_id=version.id,
                version_no=version.version_no,
                schema_version=version.schema_version,
                content_hash=version.content_hash,
                binding_role=cast(BindingRole | None, binding.binding_role),
                configuration_schema_version=binding.configuration_schema_version,
                configuration=cast(
                    dict[str, JsonValue] | None, binding.configuration_json
                ),
                model_binding_snapshot_id=(
                    model_snapshot.id if model_snapshot is not None else None
                ),
                model_binding_snapshot_hash=(
                    model_snapshot.snapshot_hash if model_snapshot is not None else None
                ),
            )
        )
    await _ensure_no_recursive_child_reference(
        session,
        tenant_id=tenant_id,
        root_agent_id=root_agent_id,
        initial_snapshots=child_snapshots,
    )
    return resolved


async def _resolve_child_agent(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    root_agent_id: UUID,
    binding: AgentBindingModel,
) -> tuple[ResolvedSnapshotBinding, AgentSnapshotModel]:
    if binding.version_policy != "resolve_on_publish":
        raise resource_state_conflict(
            "Child Agent bindings must resolve an immutable Snapshot on publish."
        )
    if binding.resource_id == root_agent_id:
        raise resource_state_conflict(
            "Agent Snapshot dependencies cannot be recursive."
        )
    child = await session.scalar(
        select(AgentDefinitionModel).where(
            AgentDefinitionModel.tenant_id == tenant_id,
            AgentDefinitionModel.id == binding.resource_id,
            AgentDefinitionModel.status == "ACTIVE",
            AgentDefinitionModel.deleted_at.is_(None),
        )
    )
    if child is None:
        raise resource_state_conflict(
            "A child Agent is not enabled and publishable in this tenant."
        )
    row = (
        await session.execute(
            select(AgentVersionModel, AgentSnapshotModel)
            .join(
                AgentSnapshotModel,
                and_(
                    AgentSnapshotModel.tenant_id == AgentVersionModel.tenant_id,
                    AgentSnapshotModel.agent_version_id == AgentVersionModel.id,
                ),
            )
            .where(
                AgentVersionModel.tenant_id == tenant_id,
                AgentVersionModel.agent_id == child.id,
            )
            .order_by(AgentVersionModel.version_no.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        raise resource_state_conflict(
            "A child Agent must have an immutable published Snapshot."
        )
    version, snapshot = row
    return (
        ResolvedSnapshotBinding(
            resource_type="agent",
            resource_id=child.id,
            version_id=version.id,
            version_no=version.version_no,
            schema_version=snapshot.schema_version,
            content_hash=snapshot.content_hash,
            agent_snapshot_id=snapshot.id,
        ),
        snapshot,
    )


async def _ensure_no_recursive_child_reference(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    root_agent_id: UUID,
    initial_snapshots: list[AgentSnapshotModel],
) -> None:
    pending = deque(initial_snapshots)
    visited: set[UUID] = set()
    while pending:
        snapshot = pending.popleft()
        if snapshot.id in visited:
            continue
        visited.add(snapshot.id)
        for binding in _snapshot_bindings(snapshot.content_json):
            if binding.get("resource_type") != "agent":
                continue
            resource_id = binding.get("resource_id")
            if resource_id == str(root_agent_id):
                raise resource_state_conflict(
                    "Agent Snapshot dependencies cannot be recursive."
                )
            nested_snapshot_id = binding.get("agent_snapshot_id")
            if not isinstance(nested_snapshot_id, str):
                raise resource_state_conflict(
                    "A child Agent binding is missing its immutable Snapshot."
                )
            nested = await session.scalar(
                select(AgentSnapshotModel).where(
                    AgentSnapshotModel.tenant_id == tenant_id,
                    AgentSnapshotModel.id == UUID(nested_snapshot_id),
                )
            )
            if nested is None:
                raise resource_state_conflict(
                    "A child Agent Snapshot dependency is unavailable."
                )
            pending.append(nested)


async def _load_replayed_publication(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    response: Mapping[str, object],
) -> SnapshotPublicationRecord:
    version_id = response.get("agent_version_id")
    snapshot_id = response.get("snapshot_id")
    if not isinstance(version_id, str) or not isinstance(snapshot_id, str):
        raise TypeError("Snapshot idempotency replay is incomplete")
    row = (
        await session.execute(
            select(AgentVersionModel, AgentSnapshotModel)
            .join(
                AgentSnapshotModel,
                and_(
                    AgentSnapshotModel.tenant_id == AgentVersionModel.tenant_id,
                    AgentSnapshotModel.agent_version_id == AgentVersionModel.id,
                ),
            )
            .where(
                AgentVersionModel.tenant_id == tenant_id,
                AgentVersionModel.id == UUID(version_id),
                AgentSnapshotModel.id == UUID(snapshot_id),
            )
        )
    ).one_or_none()
    if row is None:
        raise resource_state_conflict(
            "The idempotent Agent Snapshot result is unavailable."
        )
    version, snapshot = row
    return SnapshotPublicationRecord(
        version=_version_record(version),
        snapshot=_snapshot_record(snapshot),
        replayed=True,
    )


def _snapshot_bindings(content: dict[str, object]) -> list[dict[str, object]]:
    bindings = content.get("bindings")
    if not isinstance(bindings, list):
        raise resource_state_conflict("An immutable Agent Snapshot is malformed.")
    parsed: list[dict[str, object]] = []
    for binding in cast(list[object], bindings):
        if not isinstance(binding, dict):
            raise resource_state_conflict("An immutable Agent Snapshot is malformed.")
        parsed.append(cast(dict[str, object], binding))
    return parsed


def _version_record(model: AgentVersionModel) -> AgentVersionRecord:
    return AgentVersionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        version_no=model.version_no,
        created_from_version_id=model.created_from_version_id,
        release_note=model.release_note,
        created_at=model.created_at,
        created_by=model.created_by,
    )


def _snapshot_record(model: AgentSnapshotModel) -> AgentSnapshotRecord:
    return AgentSnapshotRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_version_id=model.agent_version_id,
        schema_version=model.schema_version,
        content=cast(dict[str, JsonValue], model.content_json),
        content_hash=model.content_hash,
        compiler_input_hash=model.compiler_input_hash,
        created_at=model.created_at,
        created_by=model.created_by,
    )


async def _add_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    agent_id: UUID,
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action="agent.snapshot.compile",
            resource_type="agent",
            resource_id=agent_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )


def _reference_key(reference: ResourceReferenceRecord) -> tuple[int, str, str, str]:
    order = {
        "draft_binding": 0,
        "snapshot": 1,
        "deployment": 2,
        "schedule": 3,
        "session": 4,
    }
    return (
        order[reference.reference_type],
        reference.resource_type,
        str(reference.resource_id),
        str(reference.version_id or ""),
    )


def _prefix_is_after(
    after: ResourceReferenceRecord | None, order: int, resource_type: str
) -> bool:
    if after is None:
        return True
    after_key = _reference_key(after)
    return (order, resource_type) >= after_key[:2]


def _same_prefix(
    after: ResourceReferenceRecord | None, order: int, resource_type: str
) -> bool:
    if after is None:
        return False
    return (order, resource_type) == _reference_key(after)[:2]


def _is_serialization_failure(error: DBAPIError) -> bool:
    return getattr(error.orig, "sqlstate", None) == "40001"


def _binding_preview_key(binding: ResolvedSnapshotBinding) -> tuple[object, ...]:
    role_order = {None: 0, "primary": 0, "fallback_1": 1, "fallback_2": 2}
    return (
        binding.resource_type,
        role_order[binding.binding_role],
        str(binding.resource_id),
        str(binding.version_id),
    )


def _snapshot_prerequisites_ready(
    *,
    runtime_type: AgentRuntimeType,
    bindings: Sequence[ResolvedSnapshotBinding],
) -> bool:
    binding_types = {binding.resource_type for binding in bindings}
    if "sandbox" not in binding_types:
        return False
    return runtime_type != "agentscope" or "model" in binding_types
