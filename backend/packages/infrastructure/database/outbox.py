"""SQLAlchemy Outbox persistence with explicit tenant transaction boundaries."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import TenantContext
from packages.domain.outbox import OutboxEvent, OutboxStatus
from packages.infrastructure.database.models import OutboxEventModel
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyOutboxWriter:
    """Add an Outbox row to the caller's already-open business transaction."""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        self._session = session
        self._context = context

    def add(self, event: OutboxEvent) -> None:
        if str(event.tenant_id) != self._context.tenant_id:
            raise ValueError("Outbox tenant does not match TenantContext")
        if event.status is not OutboxStatus.PENDING or event.attempts != 0:
            raise ValueError("new Outbox events must be PENDING with zero attempts")
        self._session.add(
            OutboxEventModel(
                id=event.id,
                tenant_id=event.tenant_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload_json=dict(event.payload),
                payload_schema_version=event.payload_schema_version,
                status=event.status.value,
                attempts=event.attempts,
                next_attempt_at=event.next_attempt_at,
                created_at=event.created_at,
                published_at=event.published_at,
            )
        )


class SqlAlchemyOutboxStore:
    """Short, committed transactions used by the process-external dispatcher."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_ready(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
        lease_duration: timedelta,
    ) -> Sequence[OutboxEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            ready = or_(
                and_(
                    OutboxEventModel.status == OutboxStatus.PENDING.value,
                    OutboxEventModel.next_attempt_at <= now,
                ),
                and_(
                    OutboxEventModel.status == OutboxStatus.PUBLISHING.value,
                    OutboxEventModel.next_attempt_at <= now,
                ),
            )
            result = await uow.session.execute(
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.tenant_id == UUID(context.tenant_id),
                    ready,
                )
                .order_by(
                    OutboxEventModel.next_attempt_at,
                    OutboxEventModel.created_at,
                    OutboxEventModel.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = list(result.scalars())
            events: list[OutboxEvent] = []
            for row in rows:
                row.status = OutboxStatus.PUBLISHING.value
                row.attempts += 1
                row.next_attempt_at = now + lease_duration
                events.append(_to_domain(row))
            return tuple(events)

    async def mark_published(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            row = await self._locked_row(uow.session, context, event_id)
            row.status = OutboxStatus.PUBLISHED.value
            row.published_at = now
            row.next_attempt_at = now

    async def mark_retry(
        self,
        context: TenantContext,
        event_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            row = await self._locked_row(uow.session, context, event_id)
            row.status = OutboxStatus.PENDING.value
            row.next_attempt_at = next_attempt_at

    async def mark_dead(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            row = await self._locked_row(uow.session, context, event_id)
            row.status = OutboxStatus.DEAD.value
            row.next_attempt_at = now

    async def _locked_row(
        self, session: AsyncSession, context: TenantContext, event_id: UUID
    ) -> OutboxEventModel:
        result = await session.execute(
            select(OutboxEventModel)
            .where(
                OutboxEventModel.id == event_id,
                OutboxEventModel.tenant_id == UUID(context.tenant_id),
                OutboxEventModel.status == OutboxStatus.PUBLISHING.value,
            )
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise RuntimeError("Outbox event is not in PUBLISHING state")
        return row


def _to_domain(row: OutboxEventModel) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=row.payload_json,
        payload_schema_version=row.payload_schema_version,
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        published_at=row.published_at,
    )
