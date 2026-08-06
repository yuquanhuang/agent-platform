"""Transactional Outbox writer tests without an external database."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.contracts.public import SubjectType, TenantContext
from packages.domain.outbox import OutboxEvent, OutboxStatus
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 6, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="22222222-2222-4222-8222-222222222222",
        auth_time=NOW,
        request_id="req-writer",
        trace_id="trace-writer",
    )


def outbox_event(tenant_id: UUID = TENANT_ID) -> OutboxEvent:
    return OutboxEvent(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        tenant_id=tenant_id,
        aggregate_type="probe",
        aggregate_id=UUID("44444444-4444-4444-8444-444444444444"),
        event_type="platform_probe_requested.v1",
        payload={"version": 1},
        payload_schema_version=1,
        status=OutboxStatus.PENDING,
        attempts=0,
        next_attempt_at=NOW,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_writer_joins_callers_session_without_committing() -> None:
    session = AsyncSession()
    try:
        writer = SqlAlchemyOutboxWriter(session, context())

        writer.add(outbox_event())

        assert len(session.new) == 1
        assert session.in_transaction() is True
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_writer_rejects_cross_tenant_event() -> None:
    session = AsyncSession()
    try:
        writer = SqlAlchemyOutboxWriter(session, context())

        with pytest.raises(ValueError, match="tenant"):
            writer.add(outbox_event(UUID("55555555-5555-4555-8555-555555555555")))
    finally:
        await session.close()
