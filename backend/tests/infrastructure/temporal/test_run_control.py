"""Temporal Run Signal adapter tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from packages.contracts.temporal import ApprovalDecidedSignal, CancelRunSignal
from packages.infrastructure.temporal import TemporalRunWorkflowControl

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
APPROVAL_ID = UUID("33333333-3333-4333-8333-333333333333")
DECISION_ID = UUID("44444444-4444-4444-8444-444444444444")


class FakeHandle:
    def __init__(self) -> None:
        self.arguments: tuple[object, ...] = ()
        self.keyword_arguments: dict[str, object] = {}
        self.description: object = SimpleNamespace(
            id=f"run/{TENANT_ID}/{RUN_ID}",
            run_id="temporal-run-1",
            status=WorkflowExecutionStatus.RUNNING,
        )

    async def signal(self, *args: object, **kwargs: object) -> None:
        self.arguments = args
        self.keyword_arguments = kwargs

    async def describe(self, **kwargs: object) -> object:
        self.keyword_arguments = kwargs
        if isinstance(self.description, Exception):
            raise self.description
        return self.description


class FakeClient:
    def __init__(self) -> None:
        self.workflow_id = ""
        self.handle = FakeHandle()

    def get_workflow_handle_for(
        self, workflow: object, workflow_id: str, **kwargs: Any
    ) -> FakeHandle:
        del workflow, kwargs
        self.workflow_id = workflow_id
        return self.handle


@pytest.mark.asyncio
async def test_cancel_signal_targets_deterministic_tenant_run_workflow() -> None:
    client = FakeClient()
    control = TemporalRunWorkflowControl(cast(Client, client))
    signal = CancelRunSignal(
        signal_id="cancel-1",
        requested_by=RUN_ID,
        requested_at=datetime(2026, 8, 8, tzinfo=UTC),
        reason="stop",
    )

    await control.signal_cancel(tenant_id=TENANT_ID, run_id=RUN_ID, signal=signal)

    assert client.workflow_id == f"run/{TENANT_ID}/{RUN_ID}"
    assert client.handle.arguments[1] == signal


@pytest.mark.asyncio
async def test_approval_signal_targets_deterministic_tenant_run_workflow() -> None:
    client = FakeClient()
    control = TemporalRunWorkflowControl(cast(Client, client))
    signal = ApprovalDecidedSignal(
        signal_id="approval-decision-1",
        approval_id=APPROVAL_ID,
        decision_id=DECISION_ID,
        decision="REJECTED",
        ticket_ref=None,
        decided_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    await control.signal_approval(tenant_id=TENANT_ID, run_id=RUN_ID, signal=signal)

    assert client.workflow_id == f"run/{TENANT_ID}/{RUN_ID}"
    assert client.handle.arguments[1] == signal


@pytest.mark.asyncio
async def test_describe_returns_temporal_execution_mapping() -> None:
    client = FakeClient()
    control = TemporalRunWorkflowControl(cast(Client, client))

    result = await control.describe_run(tenant_id=TENANT_ID, run_id=RUN_ID)

    assert result is not None
    assert result.workflow_id == f"run/{TENANT_ID}/{RUN_ID}"
    assert result.temporal_run_id == "temporal-run-1"
    assert result.status == "RUNNING"


@pytest.mark.asyncio
async def test_describe_treats_not_found_as_missing_workflow() -> None:
    client = FakeClient()
    client.handle.description = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    control = TemporalRunWorkflowControl(cast(Client, client))

    assert await control.describe_run(tenant_id=TENANT_ID, run_id=RUN_ID) is None
