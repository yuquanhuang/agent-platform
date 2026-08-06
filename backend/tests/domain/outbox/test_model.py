"""Outbox domain retry and invariant tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.domain.outbox import OutboxEvent, OutboxStatus, retry_delay


def test_retry_delay_is_deterministic_and_bounded() -> None:
    assert retry_delay(1) == timedelta(seconds=1)
    assert retry_delay(4) == timedelta(seconds=8)
    assert retry_delay(20) == timedelta(minutes=5)


def test_outbox_event_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OutboxEvent(
            id=UUID("11111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
            aggregate_type="probe",
            aggregate_id=UUID("33333333-3333-4333-8333-333333333333"),
            event_type="platform_probe_requested.v1",
            payload={},
            payload_schema_version=1,
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=datetime(2026, 8, 6),  # noqa: DTZ001 - invalid by design
            created_at=datetime(2026, 8, 6, tzinfo=UTC),
        )
