"""Async engine, session and tenant binding tests."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.database import uow as uow_module
from packages.infrastructure.database.public import (
    TENANT_SETTING_NAME,
    bind_tenant_context,
    create_database_engine,
    create_session_factory,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


def tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id="11111111-1111-4111-8111-111111111111",
        subject_type=SubjectType.USER,
        subject_id="22222222-2222-4222-8222-222222222222",
        membership_version=1,
        auth_time=datetime(2026, 8, 5, tzinfo=UTC),
        request_id="req-1",
        trace_id="trace-1",
    )


@pytest.mark.asyncio
async def test_engine_and_session_factory_use_asyncpg_and_non_expiring_sessions() -> (
    None
):
    engine = create_database_engine(
        SecretStr("postgresql+asyncpg://user:password@localhost/agent_platform"),
        service_name="api",
    )
    factory = create_session_factory(engine)
    session = factory()

    try:
        assert engine.url.drivername == "postgresql+asyncpg"
        assert session.sync_session.expire_on_commit is False
        assert session.sync_session.autoflush is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.parametrize(
    "database_dsn",
    [
        "postgresql://user:password@localhost/agent_platform",
        "sqlite+aiosqlite:///:memory:",
    ],
)
def test_engine_rejects_non_asyncpg_database_urls(database_dsn: str) -> None:
    with pytest.raises(ValueError, match=r"postgresql\+asyncpg"):
        create_database_engine(SecretStr(database_dsn), service_name="api")


@pytest.mark.asyncio
async def test_tenant_binding_requires_an_active_transaction() -> None:
    session = cast(AsyncSession, MagicMock(spec=AsyncSession))
    in_transaction = cast(MagicMock, session.in_transaction)
    in_transaction.return_value = False

    with pytest.raises(RuntimeError, match="active transaction"):
        await bind_tenant_context(session, tenant_context())


@pytest.mark.asyncio
async def test_tenant_binding_uses_parameterized_transaction_local_setting() -> None:
    fake_session = MagicMock(spec=AsyncSession)
    fake_session.info = {}
    session = cast(AsyncSession, fake_session)
    in_transaction = cast(MagicMock, session.in_transaction)
    execute = cast(AsyncMock, session.execute)
    in_transaction.return_value = True
    context = tenant_context()
    await bind_tenant_context(session, context)

    assert execute.await_args is not None
    statement, parameters = execute.await_args.args
    assert str(statement) == "SELECT set_config(:setting_name, :tenant_id, true)"
    assert parameters == {
        "setting_name": TENANT_SETTING_NAME,
        "tenant_id": context.tenant_id,
    }
    assert session.info["tenant_context"] == context


@pytest.mark.asyncio
async def test_tenant_unit_of_work_binds_then_commits_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = MagicMock(spec=AsyncSession)
    fake_session.begin = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()
    fake_session.close = AsyncMock()
    session = cast(AsyncSession, fake_session)
    begin = cast(AsyncMock, session.begin)
    commit = cast(AsyncMock, session.commit)
    rollback = cast(AsyncMock, session.rollback)
    close = cast(AsyncMock, session.close)
    factory_mock = MagicMock(return_value=session)
    factory = cast(async_sessionmaker[AsyncSession], factory_mock)
    binder = AsyncMock()
    monkeypatch.setattr(uow_module, "bind_tenant_context", binder)

    context = tenant_context()
    unit_of_work = TenantUnitOfWork(factory, context)

    async with unit_of_work as active:
        assert active.session is session

    begin.assert_awaited_once_with()
    binder.assert_awaited_once_with(session, context)
    commit.assert_awaited_once_with()
    rollback.assert_not_awaited()
    close.assert_awaited_once_with()
    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.session


@pytest.mark.asyncio
async def test_tenant_unit_of_work_rolls_back_on_use_case_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = MagicMock(spec=AsyncSession)
    fake_session.begin = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()
    fake_session.close = AsyncMock()
    session = cast(AsyncSession, fake_session)
    rollback = cast(AsyncMock, session.rollback)
    commit = cast(AsyncMock, session.commit)
    close = cast(AsyncMock, session.close)
    factory = cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session))
    monkeypatch.setattr(uow_module, "bind_tenant_context", AsyncMock())

    with pytest.raises(RuntimeError, match="use case failed"):
        async with TenantUnitOfWork(factory, tenant_context()):
            raise RuntimeError("use case failed")

    rollback.assert_awaited_once_with()
    commit.assert_not_awaited()
    close.assert_awaited_once_with()
