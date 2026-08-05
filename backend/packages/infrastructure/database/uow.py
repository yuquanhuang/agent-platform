"""Application-owned tenant transaction boundary."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import TenantContext
from packages.infrastructure.database.tenant import bind_tenant_context


class TenantUnitOfWork:
    """Own one AsyncSession and transaction for a tenant-scoped use case."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_context: TenantContext,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_context = tenant_context
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("TenantUnitOfWork is not active")
        return self._session

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        self._session = session
        try:
            await session.begin()
            await bind_tenant_context(session, self._tenant_context)
        except BaseException:
            await session.rollback()
            await session.close()
            self._session = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
