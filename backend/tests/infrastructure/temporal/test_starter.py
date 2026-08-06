"""Outbox-to-Temporal strict mapping and idempotency tests."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from packages.contracts.temporal import TemporalWorkerKind
from packages.domain.outbox import OutboxEvent, OutboxStatus
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.temporal import (
    PROBE_REQUESTED_EVENT_TYPE,
    TemporalProbeStarter,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PROBE_ID = UUID("22222222-2222-4222-8222-222222222222")


def event(*, tenant_id: UUID = TENANT_ID) -> OutboxEvent:
    return OutboxEvent(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        tenant_id=tenant_id,
        aggregate_type="probe",
        aggregate_id=PROBE_ID,
        event_type=PROBE_REQUESTED_EVENT_TYPE,
        payload={
            "tenant_id": str(TENANT_ID),
            "probe_id": str(PROBE_ID),
            "worker_kind": TemporalWorkerKind.RUN.value,
            "request_id": "req-probe",
            "trace_id": "trace-probe",
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=datetime(2026, 8, 6, tzinfo=UTC),
        created_at=datetime(2026, 8, 6, tzinfo=UTC),
    )


class FakeHandle:
    id = f"probe/{TENANT_ID}/{PROBE_ID}"
    result_run_id = "run-1"


class FakeClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.arguments: dict[str, Any] = {}

    async def start_workflow(self, *args: object, **kwargs: object) -> FakeHandle:
        self.arguments = dict(kwargs)
        if self.duplicate:
            raise WorkflowAlreadyStartedError(
                FakeHandle.id,
                "PlatformProbeWorkflow",
                run_id="run-existing",
            )
        return FakeHandle()


@pytest.mark.asyncio
async def test_starter_routes_run_probe_and_reuses_deterministic_id() -> None:
    client = FakeClient()
    starter = TemporalProbeStarter(cast(Client, client), PlatformMetrics())

    result = await starter.start(event())

    assert result.workflow_id == FakeHandle.id
    assert result.run_id == "run-1"
    assert result.already_exists is False
    assert client.arguments["task_queue"] == "run-orchestrator"
    assert client.arguments["id"] == FakeHandle.id


@pytest.mark.asyncio
async def test_starter_treats_duplicate_workflow_id_as_success() -> None:
    starter = TemporalProbeStarter(
        cast(Client, FakeClient(duplicate=True)), PlatformMetrics()
    )

    result = await starter.start(event())

    assert result.already_exists is True
    assert result.run_id == "run-existing"


@pytest.mark.asyncio
async def test_starter_rejects_payload_tenant_mismatch_without_rpc() -> None:
    client = FakeClient()
    starter = TemporalProbeStarter(cast(Client, client), PlatformMetrics())

    with pytest.raises(ValueError, match="tenant"):
        await starter.start(
            event(tenant_id=UUID("44444444-4444-4444-8444-444444444444"))
        )

    assert client.arguments == {}
