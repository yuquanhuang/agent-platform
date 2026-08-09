"""PostgreSQL persistence for user-owned platform Sessions."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import RequestMetadata
from packages.contracts.generated.core_models import (
    SessionCreateRequest,
    SessionUpdateRequest,
)
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    MutationOutcome,
    OperationRecord,
    OperationStatus,
    SessionRecord,
    SessionStatus,
    decode_cursor,
    encode_cursor,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentDefinitionModel,
    AgentRunModel,
    AuditLogModel,
    ChatSessionModel,
    DeploymentModel,
    OperationRecordModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

SESSION_STATUSES = frozenset({"ACTIVE", "ARCHIVED", "DELETED"})


class SqlAlchemySessionStore:
    """Persist Session changes with tenant RLS and explicit user ownership."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_sessions(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        limit: int,
        cursor: str | None,
        agent_id: UUID | None,
        status: str | None,
    ) -> tuple[list[SessionRecord], str | None]:
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200")
        if status is not None and status not in SESSION_STATUSES:
            raise validation_error("Session status filter is invalid.")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            statement = (
                select(ChatSessionModel)
                .where(
                    ChatSessionModel.tenant_id == tenant_id,
                    ChatSessionModel.user_id == user_id,
                )
                .order_by(
                    ChatSessionModel.updated_at.desc(), ChatSessionModel.id.desc()
                )
            )
            if status is None:
                statement = statement.where(ChatSessionModel.status != "DELETED")
            else:
                statement = statement.where(ChatSessionModel.status == status)
            if agent_id is not None:
                statement = statement.where(ChatSessionModel.agent_id == agent_id)
            if cursor is not None:
                try:
                    updated_at, session_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error("Pagination cursor is invalid.") from exc
                statement = statement.where(
                    or_(
                        ChatSessionModel.updated_at < updated_at,
                        and_(
                            ChatSessionModel.updated_at == updated_at,
                            ChatSessionModel.id < session_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.scalars(statement.limit(limit + 1))).all()
            )
            page = rows[:limit]
            next_cursor = (
                encode_cursor(page[-1].updated_at, page[-1].id)
                if len(rows) > limit and page
                else None
            )
            return [_session_record(row) for row in page], next_cursor

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
    ) -> MutationOutcome[SessionRecord]:
        tenant_id = UUID(context.tenant_id)
        try:
            agent_id = UUID(request.agent_id)
        except ValueError as exc:
            raise validation_error("Resource identifier is invalid.") from exc
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                operation_type="session.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            agent = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update(read=True)
            )
            if (
                agent is None
                or agent.status in {"DISABLED", "DELETING", "DELETED"}
                or agent.active_deployment_id is None
            ):
                raise resource_state_conflict(
                    "The Agent has no available default Deployment."
                )
            deployment = await session.scalar(
                select(DeploymentModel).where(
                    DeploymentModel.tenant_id == tenant_id,
                    DeploymentModel.id == agent.active_deployment_id,
                    DeploymentModel.agent_id == agent_id,
                    DeploymentModel.status.in_(("ACTIVE", "DEGRADED")),
                )
            )
            if deployment is None:
                raise resource_state_conflict(
                    "The Agent has no available default Deployment."
                )
            now = datetime.now(UTC)
            model = ChatSessionModel(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                default_deployment_id=deployment.id,
                title=request.title,
                metadata_json=cast(dict[str, object], metadata_value),
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            await session.flush()
            result = _session_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                action="session.create",
                resource_id=model.id,
                metadata=metadata,
                change={
                    "agent_id": str(agent_id),
                    "default_deployment_id": str(deployment.id),
                    "metadata_fields": sorted(metadata_value),
                    "title_present": request.title is not None,
                },
            )
            await complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_session_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_session(
        self, context: TenantContext, *, user_id: UUID, session_id: UUID
    ) -> SessionRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(ChatSessionModel).where(
                    ChatSessionModel.tenant_id == tenant_id,
                    ChatSessionModel.user_id == user_id,
                    ChatSessionModel.id == session_id,
                )
            )
            return _session_record(model) if model is not None else None

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
    ) -> SessionRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            model = await _locked_session(
                session, tenant_id=tenant_id, user_id=user_id, session_id=session_id
            )
            if model is None:
                return None
            _ensure_version(model, expected_version)
            if model.status == "DELETED":
                raise resource_state_conflict("Deleted Sessions cannot be updated.")
            if "title" in request.model_fields_set:
                model.title = request.title
            if metadata_value is not None:
                model.metadata_json = cast(dict[str, object], metadata_value)
            model.resource_version += 1
            model.updated_at = datetime.now(UTC)
            await session.flush()
            result = _session_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                action="session.update",
                resource_id=session_id,
                metadata=metadata,
                change={
                    "changed_fields": sorted(request.model_fields_set),
                    "metadata_fields": (
                        sorted(metadata_value) if metadata_value is not None else []
                    ),
                },
            )
            return result

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
    ) -> MutationOutcome[SessionRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                operation_type="session.archive",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            model = await _locked_session(
                session, tenant_id=tenant_id, user_id=user_id, session_id=session_id
            )
            if model is None:
                return None
            _ensure_version(model, expected_version)
            if model.status == "DELETED":
                raise resource_state_conflict("Deleted Sessions cannot be archived.")
            if model.status == "ACTIVE":
                now = datetime.now(UTC)
                model.status = "ARCHIVED"
                model.archived_at = now
                model.updated_at = now
                model.resource_version += 1
                await session.flush()
                await _add_audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    action="session.archive",
                    resource_id=session_id,
                    metadata=metadata,
                    change={"status": "ARCHIVED"},
                )
            result = _session_record(model)
            await complete_idempotency(
                session,
                record_id,
                response_status=200,
                response_body=_session_json(result),
                response_etag=f'"rv:{result.resource_version}"',
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

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
    ) -> MutationOutcome[OperationRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                operation_type="session.delete",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            model = await _locked_session(
                session, tenant_id=tenant_id, user_id=user_id, session_id=session_id
            )
            if model is None:
                return None
            _ensure_version(model, expected_version)
            if model.status != "ARCHIVED":
                raise resource_state_conflict(
                    "Session must be archived before deletion."
                )
            active_run = await session.scalar(
                select(AgentRunModel.id).where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.status.in_(
                        (
                            "CREATED",
                            "QUEUED",
                            "PREPARING",
                            "RUNNING",
                            "WAITING_APPROVAL",
                            "CANCELLING",
                        )
                    ),
                )
            )
            if active_run is not None:
                raise resource_state_conflict(
                    "Session cannot be deleted while it has an active Run."
                )
            now = datetime.now(UTC)
            model.status = "DELETED"
            model.deleted_at = now
            model.updated_at = now
            model.resource_version += 1
            operation_model = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=user_id,
                operation_type="session.delete",
                status="SUCCEEDED",
                resource_type="session",
                resource_id=session_id,
                result_json={"session_id": str(session_id), "status": "DELETED"},
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
            session.add(operation_model)
            await session.flush()
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                action="session.delete",
                resource_id=session_id,
                metadata=metadata,
                change={"status": "DELETED", "deletion_mode": "delayed"},
            )
            accepted: dict[str, object] = {
                "operation_id": str(operation_model.id),
                "status": "ACCEPTED",
                "status_url": f"/api/v1/operations/{operation_model.id}",
            }
            await complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body=accepted,
                response_etag=None,
                response_ref=str(operation_model.id),
            )
            return MutationOutcome(value=_operation_record(operation_model))


async def _locked_session(
    session: AsyncSession, *, tenant_id: UUID, user_id: UUID, session_id: UUID
) -> ChatSessionModel | None:
    return await session.scalar(
        select(ChatSessionModel)
        .where(
            ChatSessionModel.tenant_id == tenant_id,
            ChatSessionModel.user_id == user_id,
            ChatSessionModel.id == session_id,
        )
        .with_for_update()
    )


def _ensure_version(model: ChatSessionModel, expected_version: int) -> None:
    if model.resource_version != expected_version:
        raise resource_version_conflict()


def _session_record(model: ChatSessionModel) -> SessionRecord:
    return SessionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        agent_id=model.agent_id,
        default_deployment_id=model.default_deployment_id,
        title=model.title,
        status=cast(SessionStatus, model.status),
        metadata=cast(dict[str, JsonValue], model.metadata_json),
        metadata_schema_version=model.metadata_schema_version,
        resource_version=model.resource_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
        archived_at=model.archived_at,
        deleted_at=model.deleted_at,
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


def _session_json(record: SessionRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "agent_id": str(record.agent_id),
        "user_id": str(record.user_id),
        "default_deployment_id": str(record.default_deployment_id),
        "title": record.title,
        "status": record.status,
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "archived_at": (
            record.archived_at.isoformat() if record.archived_at is not None else None
        ),
    }


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
            resource_type="session",
            resource_id=resource_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )
