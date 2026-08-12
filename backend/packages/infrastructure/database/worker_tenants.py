"""Bounded active-tenant enumeration for trusted background workers."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.database.models import TenantModel
from packages.infrastructure.database.uow import PlatformUnitOfWork


class SqlAlchemyTenantContextSource:
    """Rotate over operational tenants without issuing unscoped tenant-table work."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_subject_id: UUID,
        process_name: str,
    ) -> None:
        normalized_process_name = process_name.strip()
        if not normalized_process_name or len(normalized_process_name) > 64:
            raise ValueError("worker process_name must contain 1 to 64 characters")
        self._session_factory = session_factory
        self._service_subject_id = service_subject_id
        self._process_name = normalized_process_name
        self._cursor: UUID | None = None
        self._lock = asyncio.Lock()

    async def list_service_contexts(self, *, limit: int) -> tuple[TenantContext, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("tenant context limit must be between 1 and 500")
        async with self._lock:
            async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
                tenant_ids = await self._list_operational_tenant_ids(
                    unit_of_work.session,
                    after=self._cursor,
                    limit=limit,
                )
                if not tenant_ids and self._cursor is not None:
                    tenant_ids = await self._list_operational_tenant_ids(
                        unit_of_work.session,
                        after=None,
                        limit=limit,
                    )
            self._cursor = tenant_ids[-1] if tenant_ids else None

        now = datetime.now(UTC)
        cycle_id = uuid4().hex
        return tuple(
            TenantContext(
                tenant_id=str(tenant_id),
                subject_type=SubjectType.SERVICE,
                subject_id=str(self._service_subject_id),
                auth_time=now,
                request_id=f"{self._process_name}:{cycle_id}",
                trace_id=cycle_id,
            )
            for tenant_id in tenant_ids
        )

    @staticmethod
    async def _list_operational_tenant_ids(
        session: AsyncSession,
        *,
        after: UUID | None,
        limit: int,
    ) -> tuple[UUID, ...]:
        statement = select(TenantModel.id).where(
            TenantModel.status.in_(("ACTIVE", "DISABLED"))
        )
        if after is not None:
            statement = statement.where(TenantModel.id > after)
        rows = await session.scalars(
            statement.order_by(TenantModel.id.asc()).limit(limit)
        )
        return tuple(rows.all())
