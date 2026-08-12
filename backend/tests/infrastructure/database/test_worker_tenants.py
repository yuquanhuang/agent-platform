"""Trusted background-worker tenant enumeration tests."""

from collections.abc import Sequence
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import SubjectType
from packages.infrastructure.database import worker_tenants
from packages.infrastructure.database.worker_tenants import (
    SqlAlchemyTenantContextSource,
)

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
SERVICE_ID = UUID("99999999-9999-4999-8999-999999999999")


class ScalarRows:
    def __init__(self, values: Sequence[UUID]) -> None:
        self._values = values

    def all(self) -> Sequence[UUID]:
        return self._values


class FakeSession:
    def __init__(self, pages: list[Sequence[UUID]]) -> None:
        self.pages = pages
        self.statements: list[object] = []

    async def scalars(self, statement: object) -> ScalarRows:
        self.statements.append(statement)
        return ScalarRows(self.pages.pop(0))


class FakePlatformUnitOfWork:
    def __init__(self, session: FakeSession) -> None:
        self.session = cast(AsyncSession, session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_source_rotates_operational_tenants_and_wraps_after_last_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession([[TENANT_A, TENANT_B], [], [TENANT_A]])

    def unit_of_work_factory(
        _factory: async_sessionmaker[AsyncSession],
    ) -> FakePlatformUnitOfWork:
        return FakePlatformUnitOfWork(fake_session)

    monkeypatch.setattr(
        worker_tenants,
        "PlatformUnitOfWork",
        unit_of_work_factory,
    )
    source = SqlAlchemyTenantContextSource(
        cast(async_sessionmaker[AsyncSession], object()),
        service_subject_id=SERVICE_ID,
        process_name="event-worker",
    )

    first = await source.list_service_contexts(limit=2)
    wrapped = await source.list_service_contexts(limit=2)

    assert [context.tenant_id for context in first] == [str(TENANT_A), str(TENANT_B)]
    assert [context.tenant_id for context in wrapped] == [str(TENANT_A)]
    assert all(context.subject_type is SubjectType.SERVICE for context in first)
    assert all(context.subject_id == str(SERVICE_ID) for context in first)
    assert first[0].trace_id == first[1].trace_id
    assert first[0].request_id.startswith("event-worker:")
    assert len(fake_session.statements) == 3
    rendered = [str(statement) for statement in fake_session.statements]
    assert all("tenant.status" in statement for statement in rendered)
    assert "tenant.id >" not in rendered[0]
    assert "tenant.id >" in rendered[1]
    assert "tenant.id >" not in rendered[2]


@pytest.mark.asyncio
async def test_source_rejects_unbounded_tenant_requests() -> None:
    source = SqlAlchemyTenantContextSource(
        cast(async_sessionmaker[AsyncSession], object()),
        service_subject_id=SERVICE_ID,
        process_name="reconciliation-worker",
    )

    with pytest.raises(ValueError, match="between 1 and 500"):
        await source.list_service_contexts(limit=501)
