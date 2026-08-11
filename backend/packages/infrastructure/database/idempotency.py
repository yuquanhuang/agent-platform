"""Shared durable idempotency claim and completion primitives."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.contracts.public import (
    idempotency_key_reused,
    resource_state_conflict,
)
from packages.domain.public import IdempotencyReplay
from packages.infrastructure.database.models import IdempotencyRecordModel

IDEMPOTENCY_TTL = timedelta(hours=24)


async def get_idempotency_replay(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    actor_id: UUID,
    operation_type: str,
    idempotency_key: str,
    request_hash: str,
) -> IdempotencyReplay | None:
    """Return a completed replay before expensive external validation starts."""

    tenant_filter = (
        IdempotencyRecordModel.tenant_id.is_(None)
        if tenant_id is None
        else IdempotencyRecordModel.tenant_id == tenant_id
    )
    record = await session.scalar(
        select(IdempotencyRecordModel).where(
            tenant_filter,
            IdempotencyRecordModel.actor_id == actor_id,
            IdempotencyRecordModel.operation_type == operation_type,
            IdempotencyRecordModel.idempotency_key == idempotency_key,
        )
    )
    if record is None or record.expires_at <= datetime.now(UTC):
        return None
    if record.request_hash != request_hash:
        raise idempotency_key_reused()
    if record.status != "COMPLETED" or record.response_body_json is None:
        raise resource_state_conflict(
            "The original idempotent request is still running."
        )
    return IdempotencyReplay(
        response_status=record.response_status,
        response_body=record.response_body_json,
        response_etag=record.response_etag,
    )


async def claim_idempotency(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    actor_id: UUID,
    operation_type: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[UUID, IdempotencyReplay | None]:
    """Claim a scoped key or return the completed response for a safe replay."""

    now = datetime.now(UTC)
    statement = (
        postgresql_insert(IdempotencyRecordModel)
        .values(
            tenant_id=tenant_id,
            actor_id=actor_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expires_at=now + IDEMPOTENCY_TTL,
        )
        .on_conflict_do_nothing(
            index_elements=[
                IdempotencyRecordModel.tenant_id,
                IdempotencyRecordModel.actor_id,
                IdempotencyRecordModel.operation_type,
                IdempotencyRecordModel.idempotency_key,
            ]
        )
        .returning(IdempotencyRecordModel.id)
    )
    record_id = (await session.execute(statement)).scalar_one_or_none()
    if record_id is not None:
        return record_id, None

    tenant_filter = (
        IdempotencyRecordModel.tenant_id.is_(None)
        if tenant_id is None
        else IdempotencyRecordModel.tenant_id == tenant_id
    )
    record = await session.scalar(
        select(IdempotencyRecordModel)
        .where(
            tenant_filter,
            IdempotencyRecordModel.actor_id == actor_id,
            IdempotencyRecordModel.operation_type == operation_type,
            IdempotencyRecordModel.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if record is None:
        raise RuntimeError("idempotency conflict did not resolve to a record")
    if record.expires_at <= now:
        record.request_hash = request_hash
        record.status = "IN_PROGRESS"
        record.response_status = 200
        record.response_body_json = None
        record.response_etag = None
        record.response_ref = None
        record.expires_at = now + IDEMPOTENCY_TTL
        record.updated_at = now
        return record.id, None
    if record.request_hash != request_hash:
        raise idempotency_key_reused()
    if record.status != "COMPLETED" or record.response_body_json is None:
        raise resource_state_conflict(
            "The original idempotent request is still running."
        )
    return record.id, IdempotencyReplay(
        response_status=record.response_status,
        response_body=record.response_body_json,
        response_etag=record.response_etag,
    )


async def complete_idempotency(
    session: AsyncSession,
    record_id: UUID,
    *,
    response_status: int,
    response_body: dict[str, object],
    response_etag: str | None,
    response_ref: str | None,
) -> None:
    await session.execute(
        update(IdempotencyRecordModel)
        .where(IdempotencyRecordModel.id == record_id)
        .values(
            status="COMPLETED",
            response_status=response_status,
            response_body_json=response_body,
            response_etag=response_etag,
            response_ref=response_ref,
            updated_at=datetime.now(UTC),
        )
    )
