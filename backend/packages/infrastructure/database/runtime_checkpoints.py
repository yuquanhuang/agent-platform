"""PostgreSQL metadata store for durable AgentScope checkpoints."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.temporal import RunExecutionRequest
from packages.contracts.public import TenantContext, dependency_unavailable
from packages.infrastructure.database.models import (
    RunAttemptModel,
    RuntimeCheckpointModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork
from packages.runtimes.agentscope import (
    AgentScopeCheckpointMetadataStore,
    AgentScopeCheckpointRecord,
)


class SqlAlchemyAgentScopeCheckpointStore(AgentScopeCheckpointMetadataStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        content_hash: str,
        size_bytes: int,
        fencing_token_hash: str,
        expires_at: datetime,
    ) -> AgentScopeCheckpointRecord:
        tenant_id = UUID(context.tenant_id)
        if tenant_id != request.tenant_id or size_bytes < 1:
            raise ValueError("AgentScope checkpoint identity is invalid")
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {
                        "lock_key": (
                            f"runtime-checkpoint/{tenant_id}/{request.run_id}/"
                            f"{request.execution_attempt}"
                        )
                    },
                )
                attempt = await session.scalar(
                    select(RunAttemptModel).where(
                        RunAttemptModel.tenant_id == tenant_id,
                        RunAttemptModel.run_id == request.run_id,
                        RunAttemptModel.attempt_no == request.execution_attempt,
                        RunAttemptModel.fencing_token_hash == fencing_token_hash,
                        RunAttemptModel.status.in_(("STARTING", "RUNNING")),
                    )
                )
                if attempt is None:
                    raise ValueError(
                        "AgentScope checkpoint fencing authority is unavailable"
                    )
                last_sequence = await session.scalar(
                    select(func.max(RuntimeCheckpointModel.sequence_no)).where(
                        RuntimeCheckpointModel.tenant_id == tenant_id,
                        RuntimeCheckpointModel.run_id == request.run_id,
                        RuntimeCheckpointModel.execution_attempt
                        == request.execution_attempt,
                    )
                )
                sequence_no = int(last_sequence or 0) + 1
                checkpoint_id = uuid4()
                state_ref = (
                    f"state://tenant/{tenant_id}/run/{request.run_id}/"
                    f"attempt/{request.execution_attempt}/checkpoint/{checkpoint_id}"
                )
                model = RuntimeCheckpointModel(
                    id=checkpoint_id,
                    tenant_id=tenant_id,
                    run_id=request.run_id,
                    execution_attempt=request.execution_attempt,
                    sequence_no=sequence_no,
                    state_ref=state_ref,
                    object_key=(
                        f"runtime-checkpoints/{tenant_id}/{request.run_id}/"
                        f"{request.execution_attempt}/{checkpoint_id}.json"
                    ),
                    content_hash=content_hash,
                    size_bytes=size_bytes,
                    fencing_token_hash=fencing_token_hash,
                    status="PENDING",
                    created_at=datetime.now(UTC),
                    available_at=None,
                    expires_at=expires_at,
                )
                if model.expires_at <= model.created_at:
                    raise ValueError("AgentScope checkpoint expiry is invalid")
                session.add(model)
                await session.flush()
                return _record(model)
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "AgentScope checkpoint metadata store is unavailable."
            ) from error

    async def mark_available(
        self,
        context: TenantContext,
        *,
        checkpoint_id: UUID,
        available_at: datetime,
    ) -> AgentScopeCheckpointRecord:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                row = await unit.session.scalar(
                    select(RuntimeCheckpointModel)
                    .where(
                        RuntimeCheckpointModel.tenant_id == UUID(context.tenant_id),
                        RuntimeCheckpointModel.id == checkpoint_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("AgentScope checkpoint is unavailable")
                if row.status == "AVAILABLE":
                    return _record(row)
                if row.status != "PENDING":
                    raise ValueError("AgentScope checkpoint cannot become available")
                row.status = "AVAILABLE"
                row.available_at = available_at
                await unit.session.flush()
                return _record(row)
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "AgentScope checkpoint metadata store is unavailable."
            ) from error

    async def mark_failed(
        self,
        context: TenantContext,
        *,
        checkpoint_id: UUID,
    ) -> None:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                row = await unit.session.scalar(
                    select(RuntimeCheckpointModel)
                    .where(
                        RuntimeCheckpointModel.tenant_id == UUID(context.tenant_id),
                        RuntimeCheckpointModel.id == checkpoint_id,
                    )
                    .with_for_update()
                )
                if row is None or row.status == "FAILED":
                    return
                if row.status != "PENDING":
                    raise ValueError("Available AgentScope checkpoint cannot fail")
                row.status = "FAILED"
                await unit.session.flush()
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "AgentScope checkpoint metadata store is unavailable."
            ) from error

    async def load_latest(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        fencing_token_hash: str,
        now: datetime,
    ) -> AgentScopeCheckpointRecord | None:
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                row = await unit.session.scalar(
                    select(RuntimeCheckpointModel)
                    .where(
                        RuntimeCheckpointModel.tenant_id == request.tenant_id,
                        RuntimeCheckpointModel.run_id == request.run_id,
                        RuntimeCheckpointModel.execution_attempt
                        == request.execution_attempt,
                        RuntimeCheckpointModel.fencing_token_hash == fencing_token_hash,
                        RuntimeCheckpointModel.status == "AVAILABLE",
                        RuntimeCheckpointModel.expires_at > now,
                    )
                    .order_by(RuntimeCheckpointModel.sequence_no.desc())
                    .limit(1)
                )
                return _record(row) if row is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "AgentScope checkpoint metadata store is unavailable."
            ) from error


def _record(row: RuntimeCheckpointModel) -> AgentScopeCheckpointRecord:
    return AgentScopeCheckpointRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        execution_attempt=row.execution_attempt,
        sequence_no=row.sequence_no,
        state_ref=row.state_ref,
        object_key=row.object_key,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        fencing_token_hash=row.fencing_token_hash,
        status=cast(Literal["PENDING", "AVAILABLE", "FAILED"], row.status),
        created_at=row.created_at,
        available_at=row.available_at,
        expires_at=row.expires_at,
    )
