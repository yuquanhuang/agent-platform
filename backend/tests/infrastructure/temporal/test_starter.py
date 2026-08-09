"""Outbox-to-Temporal strict mapping and idempotency tests."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    PublishAgentWorkflowInput,
    TemporalWorkerKind,
)
from packages.domain.outbox import OutboxEvent, OutboxStatus
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.temporal import (
    PROBE_REQUESTED_EVENT_TYPE,
    TemporalProbeStarter,
    TemporalReleaseStarter,
    TemporalRunStarter,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PROBE_ID = UUID("22222222-2222-4222-8222-222222222222")
RELEASE_ID = UUID("55555555-5555-4555-8555-555555555555")
OPERATION_ID = UUID("66666666-6666-4666-8666-666666666666")
AGENT_ID = UUID("77777777-7777-4777-8777-777777777777")
SNAPSHOT_ID = UUID("99999999-9999-4999-8999-999999999999")
AGENT_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


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


def release_event(*, aggregate_id: UUID = RELEASE_ID) -> OutboxEvent:
    return OutboxEvent(
        id=UUID("88888888-8888-4888-8888-888888888888"),
        tenant_id=TENANT_ID,
        aggregate_type="release",
        aggregate_id=aggregate_id,
        event_type="agent.release_requested.v1",
        payload={
            "tenant_id": str(TENANT_ID),
            "release_id": str(RELEASE_ID),
            "operation_id": str(OPERATION_ID),
            "agent_id": str(AGENT_ID),
            "expected_agent_version": 3,
            "runtime_targets": ["rt_agentscope_default"],
            "run_smoke_test": True,
            "activate_on_success": True,
            "request_id": "req-release",
            "trace_id": "trace-release",
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=datetime(2026, 8, 7, tzinfo=UTC),
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


def rollback_event() -> OutboxEvent:
    event = release_event()
    return OutboxEvent(
        id=event.id,
        tenant_id=event.tenant_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        payload={
            **event.payload,
            "release_kind": "ROLLBACK",
            "expected_agent_version": None,
            "requested_snapshot_id": str(SNAPSHOT_ID),
        },
        payload_schema_version=event.payload_schema_version,
        status=event.status,
        attempts=event.attempts,
        next_attempt_at=event.next_attempt_at,
        created_at=event.created_at,
    )


def run_event(*, aggregate_id: UUID = AGENT_RUN_ID) -> OutboxEvent:
    return OutboxEvent(
        id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        tenant_id=TENANT_ID,
        aggregate_type="run",
        aggregate_id=aggregate_id,
        event_type="agent.run_requested.v1",
        payload={
            "tenant_id": str(TENANT_ID),
            "run_id": str(AGENT_RUN_ID),
            "initial_execution_attempt": 1,
            "request_id": "req-run",
            "trace_id": "trace-run",
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=datetime(2026, 8, 8, tzinfo=UTC),
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


class FakeHandle:
    id = f"probe/{TENANT_ID}/{PROBE_ID}"
    result_run_id = "run-1"


class FakeClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.positional_arguments: tuple[object, ...] = ()
        self.arguments: dict[str, Any] = {}

    async def start_workflow(self, *args: object, **kwargs: object) -> FakeHandle:
        self.positional_arguments = args
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


@pytest.mark.asyncio
async def test_release_starter_uses_control_queue_and_deterministic_id() -> None:
    client = FakeClient()
    starter = TemporalReleaseStarter(cast(Client, client), PlatformMetrics())

    result = await starter.start(release_event())

    expected = f"publish/{TENANT_ID}/{RELEASE_ID}"
    assert result.workflow_id == FakeHandle.id
    assert client.arguments["id"] == expected
    assert client.arguments["task_queue"] == "control-plane"


@pytest.mark.asyncio
async def test_release_starter_rejects_aggregate_mismatch_without_rpc() -> None:
    client = FakeClient()
    starter = TemporalReleaseStarter(cast(Client, client), PlatformMetrics())

    with pytest.raises(ValueError, match="identity"):
        await starter.start(
            release_event(aggregate_id=UUID("99999999-9999-4999-8999-999999999999"))
        )

    assert client.arguments == {}


@pytest.mark.asyncio
async def test_release_starter_preserves_rollback_snapshot_source() -> None:
    client = FakeClient()
    starter = TemporalReleaseStarter(cast(Client, client), PlatformMetrics())

    await starter.start(rollback_event())

    workflow_input = cast(PublishAgentWorkflowInput, client.positional_arguments[1])
    assert workflow_input.release_kind == "ROLLBACK"
    assert workflow_input.expected_agent_version is None
    assert workflow_input.requested_snapshot_id == SNAPSHOT_ID


@pytest.mark.asyncio
async def test_run_starter_uses_run_queue_and_deterministic_id() -> None:
    client = FakeClient()
    starter = TemporalRunStarter(cast(Client, client), PlatformMetrics())

    await starter.start(run_event())

    expected = f"run/{TENANT_ID}/{AGENT_RUN_ID}"
    workflow_input = cast(AgentRunWorkflowInput, client.positional_arguments[1])
    assert client.arguments["id"] == expected
    assert client.arguments["task_queue"] == "run-orchestrator"
    assert workflow_input.run_id == AGENT_RUN_ID


@pytest.mark.asyncio
async def test_run_starter_rejects_aggregate_mismatch_without_rpc() -> None:
    client = FakeClient()
    starter = TemporalRunStarter(cast(Client, client), PlatformMetrics())

    with pytest.raises(ValueError, match="identity"):
        await starter.start(
            run_event(aggregate_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
        )

    assert client.arguments == {}
