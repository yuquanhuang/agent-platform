"""PostgreSQL persistence for Agent Draft definitions and bindings."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import RequestMetadata
from packages.contracts.generated.core_models import (
    AgentCreateRequest,
    AgentUpdateRequest,
    CopyAgentRequest,
    DisableAgentRequest,
)
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    AgentBindingRecord,
    AgentRecord,
    AgentReferenceRecord,
    AgentRuntimeType,
    AgentStatus,
    AgentVisibility,
    BindingRole,
    BindingVersionPolicy,
    MutationOutcome,
    OperationRecord,
    OperationStatus,
    decode_cursor,
    encode_cursor,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentBindingModel,
    AgentDefinitionModel,
    AuditLogModel,
    OperationRecordModel,
    ResourceDefinitionModel,
    ResourceVersionModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

AGENT_STATUSES = frozenset({"DRAFT", "ACTIVE", "DISABLED", "DELETING", "DELETED"})
RESOURCE_TYPE_MAP = {
    "prompt": "prompt",
    "skill": "skill",
    "mcp": "mcp",
    "model": "model_config",
    "knowledge": "knowledge",
    "sandbox": "sandbox_profile",
}


class SqlAlchemyAgentRegistry:
    """Persist Agent Draft changes inside explicit tenant transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_agents(
        self,
        context: TenantContext,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        keyword: str | None,
    ) -> tuple[list[AgentRecord], str | None]:
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200")
        if status is not None and status not in AGENT_STATUSES:
            raise validation_error("Agent status filter is invalid.")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            statement = (
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .order_by(
                    AgentDefinitionModel.created_at.desc(),
                    AgentDefinitionModel.id.desc(),
                )
            )
            if status is not None:
                statement = statement.where(AgentDefinitionModel.status == status)
            if keyword:
                statement = statement.where(
                    or_(
                        AgentDefinitionModel.code.ilike(f"%{keyword}%"),
                        AgentDefinitionModel.name.ilike(f"%{keyword}%"),
                    )
                )
            if cursor is not None:
                try:
                    created_at, agent_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error("Pagination cursor is invalid.") from exc
                statement = statement.where(
                    or_(
                        AgentDefinitionModel.created_at < created_at,
                        and_(
                            AgentDefinitionModel.created_at == created_at,
                            AgentDefinitionModel.id < agent_id,
                        ),
                    )
                )
            rows = list((await session.scalars(statement.limit(limit + 1))).all())
            page = rows[:limit]
            bindings = await _load_bindings(
                session, tenant_id=tenant_id, agent_ids=[row.id for row in page]
            )
            next_cursor = (
                encode_cursor(page[-1].created_at, page[-1].id)
                if len(rows) > limit and page
                else None
            )
            return [
                _agent_record(row, bindings.get(row.id, ())) for row in page
            ], next_cursor

    async def create_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: AgentCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            await _ensure_code_available(
                session, tenant_id=tenant_id, code=request.code
            )
            now = datetime.now(UTC)
            model = AgentDefinitionModel(
                tenant_id=tenant_id,
                code=request.code,
                name=request.name,
                description=request.description,
                runtime_type=request.runtime_type,
                visibility=request.visibility or "private",
                tags_json=list(request.tags or []),
                owner_user_id=actor_id,
                created_by=actor_id,
                updated_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            await session.flush()
            result = _agent_record(model, ())
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.create",
                resource_id=model.id,
                metadata=metadata,
                change={"code": request.code, "runtime_type": request.runtime_type},
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_agent_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_agent(
        self, context: TenantContext, *, agent_id: UUID
    ) -> AgentRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            model = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
            )
            if model is None:
                return None
            bindings = await _load_bindings(
                session, tenant_id=tenant_id, agent_ids=[agent_id]
            )
            return _agent_record(model, bindings.get(agent_id, ()))

    async def list_agent_references(
        self, context: TenantContext, *, agent_id: UUID
    ) -> list[AgentReferenceRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
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

            references: list[AgentReferenceRecord] = []
            if agent.active_deployment_id is not None:
                references.append(
                    AgentReferenceRecord(
                        resource_type="deployment",
                        resource_id=agent.active_deployment_id,
                        reference_type="deployment",
                    )
                )
            parent_agent_ids = list(
                (
                    await session.scalars(
                        select(AgentBindingModel.agent_id)
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
                            AgentBindingModel.resource_type == "agent",
                            AgentBindingModel.resource_id == agent_id,
                            AgentDefinitionModel.deleted_at.is_(None),
                        )
                        .order_by(AgentBindingModel.agent_id)
                    )
                ).all()
            )
            references.extend(
                AgentReferenceRecord(
                    resource_type="agent",
                    resource_id=parent_id,
                    reference_type="child_agent",
                )
                for parent_id in parent_agent_ids
            )
            return references

    async def update_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_version: int,
        request: AgentUpdateRequest,
        bindings: list[AgentBindingRecord] | None,
        metadata: RequestMetadata,
    ) -> AgentRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            model = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            if model.resource_version != expected_version:
                raise resource_version_conflict()
            if model.status in {"DELETING", "DELETED"}:
                raise resource_state_conflict(
                    "Agent cannot be edited from its current state."
                )
            if bindings is not None:
                await _validate_binding_targets(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    bindings=bindings,
                )
                await session.execute(
                    delete(AgentBindingModel).where(
                        AgentBindingModel.tenant_id == tenant_id,
                        AgentBindingModel.agent_id == agent_id,
                    )
                )
                _add_binding_models(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    bindings=bindings,
                )
            if request.name is not None:
                model.name = request.name
            if "description" in request.model_fields_set:
                model.description = request.description
            if request.visibility is not None:
                model.visibility = request.visibility
            if request.tags is not None:
                model.tags_json = list(request.tags)
            model.resource_version += 1
            model.updated_by = actor_id
            model.updated_at = datetime.now(UTC)
            await session.flush()
            current_bindings = (
                tuple(bindings)
                if bindings is not None
                else (
                    await _load_bindings(
                        session, tenant_id=tenant_id, agent_ids=[agent_id]
                    )
                ).get(agent_id, ())
            )
            result = _agent_record(model, current_bindings)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.update",
                resource_id=agent_id,
                metadata=metadata,
                change=_update_summary(request, current_bindings),
            )
            return result

    async def copy_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        request: CopyAgentRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.copy",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            source = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
            )
            if source is None:
                return None
            await _ensure_code_available(
                session, tenant_id=tenant_id, code=request.code
            )
            source_bindings = (
                await _load_bindings(session, tenant_id=tenant_id, agent_ids=[agent_id])
            ).get(agent_id, ())
            now = datetime.now(UTC)
            model = AgentDefinitionModel(
                tenant_id=tenant_id,
                code=request.code,
                name=request.name,
                description=source.description,
                runtime_type=source.runtime_type,
                visibility=source.visibility,
                tags_json=list(source.tags_json),
                owner_user_id=actor_id,
                created_by=actor_id,
                updated_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            await session.flush()
            _add_binding_models(
                session,
                tenant_id=tenant_id,
                agent_id=model.id,
                actor_id=actor_id,
                bindings=list(source_bindings),
            )
            await session.flush()
            result = _agent_record(model, source_bindings)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.copy",
                resource_id=model.id,
                metadata=metadata,
                change={"source_agent_id": str(agent_id), "code": request.code},
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_agent_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def set_agent_disabled(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_version: int,
        request: DisableAgentRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.disable",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            model = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            if model.resource_version != expected_version:
                raise resource_version_conflict()
            if model.status not in {"DRAFT", "ACTIVE"}:
                raise resource_state_conflict(
                    "Agent cannot be disabled from its current state."
                )
            model.status = "DISABLED"
            model.resource_version += 1
            model.updated_by = actor_id
            model.updated_at = datetime.now(UTC)
            await session.flush()
            bindings = (
                await _load_bindings(session, tenant_id=tenant_id, agent_ids=[agent_id])
            ).get(agent_id, ())
            result = _agent_record(model, bindings)
            reason_digest = _optional_text_digest(request.reason if request else None)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.disable",
                resource_id=agent_id,
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
                response_body=_agent_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def delete_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
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
                operation_type="agent.delete",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            model = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            if model.resource_version != expected_version:
                raise resource_version_conflict()
            if model.active_deployment_id is not None:
                raise resource_state_conflict(
                    "Agent has an active Deployment and cannot be deleted."
                )
            inbound_reference = await session.scalar(
                select(AgentBindingModel.id)
                .join(
                    AgentDefinitionModel,
                    and_(
                        AgentDefinitionModel.tenant_id == AgentBindingModel.tenant_id,
                        AgentDefinitionModel.id == AgentBindingModel.agent_id,
                    ),
                )
                .where(
                    AgentBindingModel.tenant_id == tenant_id,
                    AgentBindingModel.resource_type == "agent",
                    AgentBindingModel.resource_id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .limit(1)
            )
            if inbound_reference is not None:
                raise resource_state_conflict(
                    "Agent is referenced by another active Agent Draft."
                )
            now = datetime.now(UTC)
            model.status = "DELETED"
            model.deleted_at = now
            model.deleted_by = actor_id
            model.updated_at = now
            model.updated_by = actor_id
            model.resource_version += 1
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.delete",
                status="SUCCEEDED",
                resource_type="agent",
                resource_id=agent_id,
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
                action="agent.delete",
                resource_id=agent_id,
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


async def _ensure_code_available(
    session: AsyncSession, *, tenant_id: UUID, code: str
) -> None:
    duplicate = await session.scalar(
        select(AgentDefinitionModel.id).where(
            AgentDefinitionModel.tenant_id == tenant_id,
            AgentDefinitionModel.code == code,
            AgentDefinitionModel.deleted_at.is_(None),
        )
    )
    if duplicate is not None:
        raise resource_state_conflict("Agent code is already in use.")


async def _validate_binding_targets(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    bindings: list[AgentBindingRecord],
) -> None:
    if any(
        binding.resource_type == "agent" and binding.resource_id == agent_id
        for binding in bindings
    ):
        raise validation_error("Agent cannot bind to itself.")

    resource_bindings = [
        binding for binding in bindings if binding.resource_type != "agent"
    ]
    definition_ids = {binding.resource_id for binding in resource_bindings}
    definition_rows = (
        list(
            (
                await session.scalars(
                    select(ResourceDefinitionModel).where(
                        ResourceDefinitionModel.tenant_id == tenant_id,
                        ResourceDefinitionModel.id.in_(definition_ids),
                        ResourceDefinitionModel.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        if definition_ids
        else []
    )
    definitions = {row.id: row for row in definition_rows}
    agent_target_ids = {
        binding.resource_id for binding in bindings if binding.resource_type == "agent"
    }
    existing_agents: set[UUID] = (
        set(
            cast(
                list[UUID],
                (
                    await session.scalars(
                        select(AgentDefinitionModel.id).where(
                            AgentDefinitionModel.tenant_id == tenant_id,
                            AgentDefinitionModel.id.in_(agent_target_ids),
                            AgentDefinitionModel.deleted_at.is_(None),
                        )
                    )
                ).all(),
            )
        )
        if agent_target_ids
        else set()
    )
    fixed_version_ids = {
        binding.version_id
        for binding in resource_bindings
        if binding.version_id is not None
    }
    version_rows = (
        list(
            (
                await session.scalars(
                    select(ResourceVersionModel).where(
                        ResourceVersionModel.tenant_id == tenant_id,
                        ResourceVersionModel.id.in_(fixed_version_ids),
                    )
                )
            ).all()
        )
        if fixed_version_ids
        else []
    )
    versions = {row.id: row for row in version_rows}

    for binding in bindings:
        if binding.resource_type == "agent":
            if binding.resource_id not in existing_agents:
                raise resource_state_conflict(
                    "The referenced Agent is not available in this tenant."
                )
            if binding.version_policy == "fixed":
                raise resource_state_conflict(
                    "Fixed child Agent versions are unavailable before Agent publishing."
                )
            continue
        definition = definitions.get(binding.resource_id)
        expected_type = RESOURCE_TYPE_MAP.get(binding.resource_type)
        if definition is None or definition.resource_type != expected_type:
            raise resource_state_conflict(
                "The referenced resource is not available in this tenant."
            )
        if binding.version_policy == "fixed":
            if binding.version_id is None:
                raise validation_error("Fixed bindings require version_id.")
            version = versions.get(binding.version_id)
            if version is None or version.definition_id != binding.resource_id:
                raise resource_state_conflict(
                    "The fixed resource version is not available in this tenant."
                )


def _add_binding_models(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    actor_id: UUID,
    bindings: list[AgentBindingRecord],
) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            AgentBindingModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                resource_type=binding.resource_type,
                resource_id=binding.resource_id,
                version_policy=binding.version_policy,
                fixed_version_id=binding.version_id,
                binding_role=binding.binding_role,
                configuration_json=cast(
                    dict[str, object] | None, binding.configuration
                ),
                configuration_schema_version=binding.configuration_schema_version,
                created_at=now,
                created_by=actor_id,
                updated_at=now,
                updated_by=actor_id,
            )
            for binding in bindings
        ]
    )


async def _load_bindings(
    session: AsyncSession, *, tenant_id: UUID, agent_ids: list[UUID]
) -> dict[UUID, tuple[AgentBindingRecord, ...]]:
    if not agent_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(AgentBindingModel).where(
                    AgentBindingModel.tenant_id == tenant_id,
                    AgentBindingModel.agent_id.in_(agent_ids),
                )
            )
        ).all()
    )
    grouped: dict[UUID, list[AgentBindingRecord]] = {}
    for row in rows:
        grouped.setdefault(row.agent_id, []).append(_binding_record(row))
    return {
        current_agent_id: tuple(sorted(values, key=_binding_sort_key))
        for current_agent_id, values in grouped.items()
    }


def _binding_sort_key(binding: AgentBindingRecord) -> tuple[int, str, str]:
    role_order = {"primary": 0, "fallback_1": 1, "fallback_2": 2, None: 3}
    return (
        role_order[binding.binding_role],
        binding.resource_type,
        str(binding.resource_id),
    )


def _binding_record(model: AgentBindingModel) -> AgentBindingRecord:
    return AgentBindingRecord(
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        version_policy=cast(BindingVersionPolicy, model.version_policy),
        version_id=model.fixed_version_id,
        binding_role=cast(BindingRole | None, model.binding_role),
        configuration_schema_version=cast(
            Literal["model-routing/v1"] | None,
            model.configuration_schema_version,
        ),
        configuration=cast(dict[str, JsonValue] | None, model.configuration_json),
    )


def _agent_record(
    model: AgentDefinitionModel, bindings: tuple[AgentBindingRecord, ...]
) -> AgentRecord:
    return AgentRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        code=model.code,
        name=model.name,
        description=model.description,
        runtime_type=cast(AgentRuntimeType, model.runtime_type),
        visibility=cast(AgentVisibility, model.visibility),
        tags=tuple(model.tags_json),
        bindings=bindings,
        status=cast(AgentStatus, model.status),
        resource_version=model.resource_version,
        active_deployment_id=model.active_deployment_id,
        owner_user_id=model.owner_user_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


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


def _agent_json(record: AgentRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "code": record.code,
        "name": record.name,
        "description": record.description,
        "runtime_type": record.runtime_type,
        "visibility": record.visibility,
        "tags": list(record.tags),
        "bindings": [
            {
                "resource_type": binding.resource_type,
                "resource_id": str(binding.resource_id),
                "version_policy": binding.version_policy,
                "version_id": (
                    str(binding.version_id) if binding.version_id is not None else None
                ),
                "binding_role": binding.binding_role,
                "configuration_schema_version": binding.configuration_schema_version,
                "configuration": binding.configuration,
            }
            for binding in record.bindings
        ],
        "status": record.status,
        "resource_version": record.resource_version,
        "active_deployment_id": (
            str(record.active_deployment_id)
            if record.active_deployment_id is not None
            else None
        ),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _update_summary(
    request: AgentUpdateRequest, bindings: tuple[AgentBindingRecord, ...]
) -> dict[str, object]:
    summary: dict[str, object] = {"changed_fields": sorted(request.model_fields_set)}
    if "bindings" in request.model_fields_set:
        summary["binding_count"] = len(bindings)
        summary["model_roles"] = [
            binding.binding_role
            for binding in bindings
            if binding.resource_type == "model"
        ]
        fallback_error_codes: set[str] = set()
        for binding in bindings:
            if binding.configuration is None:
                continue
            raw_codes = binding.configuration.get("fallback_error_codes")
            if isinstance(raw_codes, list):
                fallback_error_codes.update(
                    code for code in raw_codes if isinstance(code, str)
                )
        summary["fallback_error_codes"] = sorted(fallback_error_codes)
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
            resource_type="agent",
            resource_id=resource_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )
