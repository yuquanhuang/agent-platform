"""Run Outbox start-result mapping tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.outbox import (
    PermanentOutboxError,
    RetryableOutboxError,
    RunWorkflowStartRecorder,
    RunWorkflowStartStore,
    WorkflowStartResult,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.outbox import OutboxEvent, OutboxStatus

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 8, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="44444444-4444-4444-8444-444444444444",
        auth_time=NOW,
        request_id="req-reconcile",
        trace_id="trace-reconcile",
    )


def event() -> OutboxEvent:
    return OutboxEvent(
        id=EVENT_ID,
        tenant_id=TENANT_ID,
        aggregate_type="agent_run",
        aggregate_id=RUN_ID,
        event_type="agent.run_requested.v1",
        payload={
            "tenant_id": str(TENANT_ID),
            "run_id": str(RUN_ID),
            "initial_execution_attempt": 1,
            "request_id": "req-run",
            "trace_id": "trace-run",
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=NOW,
        created_at=NOW,
    )


class Store:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    async def record_workflow_start(
        self, context: TenantContext, **kwargs: object
    ) -> bool:
        del context
        self.values = kwargs
        return True


@pytest.mark.asyncio
async def test_run_recorder_persists_deterministic_mapping() -> None:
    store = Store()
    recorder = RunWorkflowStartRecorder(cast(RunWorkflowStartStore, store))

    await recorder.record(
        context(),
        event(),
        WorkflowStartResult(
            workflow_id=f"run/{TENANT_ID}/{RUN_ID}",
            run_id="temporal-run-1",
            already_exists=True,
        ),
        now=NOW,
    )

    assert store.values["run_id"] == RUN_ID
    assert store.values["temporal_run_id"] == "temporal-run-1"
    assert store.values["outcome"] == "ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_run_recorder_rejects_conflicting_workflow_identity() -> None:
    recorder = RunWorkflowStartRecorder(cast(RunWorkflowStartStore, Store()))

    with pytest.raises(PermanentOutboxError, match="identity"):
        await recorder.record(
            context(),
            event(),
            WorkflowStartResult(
                workflow_id="run/other/run",
                run_id="temporal-run-1",
                already_exists=False,
            ),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_run_recorder_retries_when_temporal_run_id_is_missing() -> None:
    recorder = RunWorkflowStartRecorder(cast(RunWorkflowStartStore, Store()))

    with pytest.raises(RetryableOutboxError, match="Run ID"):
        await recorder.record(
            context(),
            event(),
            WorkflowStartResult(
                workflow_id=f"run/{TENANT_ID}/{RUN_ID}",
                run_id=None,
                already_exists=True,
            ),
            now=NOW,
        )
