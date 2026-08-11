"""Tenant-scoped, stable AuditLog query adapter."""

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.audit import AuditQueryStore
from packages.contracts.public import (
    TenantContext,
    dependency_unavailable,
    validation_error,
)
from packages.domain.public import (
    AuditLogRecord,
    AuditResult,
    decode_cursor,
    encode_cursor,
)
from packages.infrastructure.database.models import AuditLogModel
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyAuditQueryStore(AuditQueryStore):
    """Query only the caller tenant and never expose Audit metadata JSON."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_audit_logs(
        self,
        context: TenantContext,
        *,
        action: str | None,
        resource_type: str | None,
        actor_id: UUID | None,
        run_id: UUID | None,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[AuditLogRecord], str | None]:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                statement = select(AuditLogModel).where(
                    AuditLogModel.tenant_id == tenant_id
                )
                if action is not None:
                    statement = statement.where(AuditLogModel.action == action)
                if resource_type is not None:
                    statement = statement.where(
                        AuditLogModel.resource_type == resource_type
                    )
                if actor_id is not None:
                    statement = statement.where(AuditLogModel.actor_id == actor_id)
                if run_id is not None:
                    statement = statement.where(AuditLogModel.run_id == run_id)
                if occurred_from is not None:
                    statement = statement.where(
                        AuditLogModel.created_at >= occurred_from
                    )
                if occurred_to is not None:
                    statement = statement.where(AuditLogModel.created_at <= occurred_to)
                if cursor is not None:
                    try:
                        created_at, event_id = decode_cursor(cursor)
                    except ValueError as error:
                        raise validation_error(
                            "Pagination cursor is invalid."
                        ) from error
                    statement = statement.where(
                        or_(
                            AuditLogModel.created_at < created_at,
                            and_(
                                AuditLogModel.created_at == created_at,
                                AuditLogModel.id < event_id,
                            ),
                        )
                    )
                statement = statement.order_by(
                    AuditLogModel.created_at.desc(), AuditLogModel.id.desc()
                )
                rows = list(
                    (await unit.session.scalars(statement.limit(limit + 1))).all()
                )
                page = rows[:limit]
                next_cursor = (
                    encode_cursor(page[-1].created_at, page[-1].id)
                    if len(rows) > limit and page
                    else None
                )
                return [_record(row) for row in page], next_cursor
        except SQLAlchemyError as error:
            raise dependency_unavailable("Audit Store is unavailable.") from error


def _record(model: AuditLogModel) -> AuditLogRecord:
    return AuditLogRecord(
        event_id=model.id,
        tenant_id=model.tenant_id,
        occurred_at=model.created_at,
        actor_type=model.actor_type,
        actor_id=model.actor_id,
        action=model.action,
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        run_id=model.run_id,
        result=cast(AuditResult, model.result),
        trace_id=model.trace_id,
    )
