"""Stalled Run reconciliation behavior tests."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

import pytest

from packages.application.reconciliation import (
    RunReconciler,
    RunReconciliationCandidate,
    RunReconciliationStore,
    RunWorkflowExecution,
    RunWorkflowReconciliationControl,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import CancelRunSignal

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
CANCELLING_RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
TERMINAL_WORKFLOW_RUN_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="66666666-6666-4666-8666-666666666666",
        auth_time=NOW,
        request_id="req-reconcile",
        trace_id="trace-reconcile",
    )


def candidate(
    run_id: UUID, status: Literal["CREATED", "CANCELLING"]
) -> RunReconciliationCandidate:
    return RunReconciliationCandidate(
        run_id=run_id,
        tenant_id=TENANT_ID,
        status=status,
        created_by=ACTOR_ID,
        workflow_id=None,
        temporal_run_id=None,
    )


class Store:
    def __init__(self) -> None:
        self.candidates: tuple[RunReconciliationCandidate, ...] = (
            candidate(CREATED_RUN_ID, "CREATED"),
            candidate(CANCELLING_RUN_ID, "CANCELLING"),
            candidate(TERMINAL_WORKFLOW_RUN_ID, "CREATED"),
        )
        self.requeued: list[UUID] = []
        self.mapped: list[UUID] = []

    async def list_stalled_runs(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[RunReconciliationCandidate, ...]:
        del context, kwargs
        return self.candidates

    async def requeue_run_request(
        self, context: TenantContext, *, run_id: UUID, now: datetime
    ) -> bool:
        del context
        del now
        self.requeued.append(run_id)
        return True

    async def record_workflow_start(
        self, context: TenantContext, **kwargs: object
    ) -> bool:
        del context
        self.mapped.append(cast(UUID, kwargs["run_id"]))
        return True


class Control:
    def __init__(self) -> None:
        self.signals: list[CancelRunSignal] = []

    async def describe_run(
        self, *, tenant_id: UUID, run_id: UUID
    ) -> RunWorkflowExecution | None:
        assert tenant_id == TENANT_ID
        if run_id == CREATED_RUN_ID:
            return None
        return RunWorkflowExecution(
            workflow_id=f"run/{TENANT_ID}/{run_id}",
            temporal_run_id=f"temporal-{run_id}",
            status="COMPLETED" if run_id == TERMINAL_WORKFLOW_RUN_ID else "RUNNING",
        )

    async def signal_cancel(self, **kwargs: object) -> None:
        self.signals.append(cast(CancelRunSignal, kwargs["signal"]))


@pytest.mark.asyncio
async def test_reconciler_requeues_missing_workflow_and_resignals_cancellation() -> (
    None
):
    store = Store()
    control = Control()
    reconciler = RunReconciler(
        cast(RunReconciliationStore, store),
        cast(RunWorkflowReconciliationControl, control),
    )

    summary = await reconciler.reconcile_tenant_once(context(), now=NOW)

    assert summary.examined == 3
    assert summary.requests_requeued == 1
    assert summary.mappings_recorded == 2
    assert summary.cancellations_signalled == 1
    assert summary.unresolved == 1
    assert store.requeued == [CREATED_RUN_ID]
    assert store.mapped == [CANCELLING_RUN_ID, TERMINAL_WORKFLOW_RUN_ID]
    assert len(control.signals) == 1
    assert control.signals[0].requested_by == ACTOR_ID


@pytest.mark.asyncio
async def test_reconciliation_cancel_signal_is_stable_across_cycles() -> None:
    store = Store()
    store.candidates = (candidate(CANCELLING_RUN_ID, "CANCELLING"),)
    control = Control()
    reconciler = RunReconciler(
        cast(RunReconciliationStore, store),
        cast(RunWorkflowReconciliationControl, control),
    )

    await reconciler.reconcile_tenant_once(context(), now=NOW)
    await reconciler.reconcile_tenant_once(context(), now=NOW)

    assert control.signals[0].signal_id == control.signals[1].signal_id
