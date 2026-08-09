"""Idempotent reconciliation for stalled Run startup and cancellation."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from packages.application.outbox.run import RunWorkflowStartStore
from packages.contracts.public import TenantContext
from packages.contracts.temporal import CancelRunSignal

ReconciledRunStatus = Literal["CREATED", "CANCELLING"]
WorkflowExecutionStatus = Literal[
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "TERMINATED",
    "CONTINUED_AS_NEW",
    "TIMED_OUT",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class RunReconciliationCandidate:
    run_id: UUID
    tenant_id: UUID
    status: ReconciledRunStatus
    created_by: UUID
    workflow_id: str | None
    temporal_run_id: str | None


@dataclass(frozen=True, slots=True)
class RunWorkflowExecution:
    workflow_id: str
    temporal_run_id: str
    status: WorkflowExecutionStatus


class RunReconciliationStore(RunWorkflowStartStore, Protocol):
    async def list_stalled_runs(
        self,
        context: TenantContext,
        *,
        created_before: datetime,
        cancelling_before: datetime,
        limit: int,
    ) -> Sequence[RunReconciliationCandidate]: ...

    async def requeue_run_request(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        now: datetime,
    ) -> bool: ...


class RunWorkflowReconciliationControl(Protocol):
    async def describe_run(
        self, *, tenant_id: UUID, run_id: UUID
    ) -> RunWorkflowExecution | None: ...

    async def signal_cancel(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        signal: CancelRunSignal,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RunReconciliationSummary:
    examined: int = 0
    mappings_recorded: int = 0
    requests_requeued: int = 0
    cancellations_signalled: int = 0
    unresolved: int = 0


class RunReconciler:
    """Compare durable Run state with deterministic Temporal Workflow identity."""

    def __init__(
        self,
        store: RunReconciliationStore,
        control: RunWorkflowReconciliationControl,
        *,
        created_stale_after: timedelta = timedelta(minutes=2),
        cancelling_stale_after: timedelta = timedelta(minutes=2),
        batch_size: int = 50,
    ) -> None:
        if created_stale_after <= timedelta(0) or cancelling_stale_after <= timedelta(
            0
        ):
            raise ValueError("Run reconciliation thresholds must be positive")
        if batch_size < 1:
            raise ValueError("Run reconciliation batch_size must be positive")
        self._store = store
        self._control = control
        self._created_stale_after = created_stale_after
        self._cancelling_stale_after = cancelling_stale_after
        self._batch_size = batch_size

    async def reconcile_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> RunReconciliationSummary:
        candidates = await self._store.list_stalled_runs(
            context,
            created_before=now - self._created_stale_after,
            cancelling_before=now - self._cancelling_stale_after,
            limit=self._batch_size,
        )
        mappings_recorded = requests_requeued = cancellations_signalled = 0
        unresolved = 0
        for candidate in candidates:
            execution = await self._control.describe_run(
                tenant_id=candidate.tenant_id,
                run_id=candidate.run_id,
            )
            if execution is None:
                if await self._store.requeue_run_request(
                    context, run_id=candidate.run_id, now=now
                ):
                    requests_requeued += 1
                continue
            if await self._store.record_workflow_start(
                context,
                run_id=candidate.run_id,
                workflow_id=execution.workflow_id,
                temporal_run_id=execution.temporal_run_id,
                outcome="ALREADY_EXISTS",
                started_at=now,
            ):
                mappings_recorded += 1
            if execution.status != "RUNNING":
                unresolved += 1
                continue
            if candidate.status == "CANCELLING":
                await self._control.signal_cancel(
                    tenant_id=candidate.tenant_id,
                    run_id=candidate.run_id,
                    signal=CancelRunSignal(
                        signal_id=_reconciliation_signal_id(candidate),
                        requested_by=candidate.created_by,
                        requested_at=now,
                        reason=None,
                    ),
                )
                cancellations_signalled += 1
        return RunReconciliationSummary(
            examined=len(candidates),
            mappings_recorded=mappings_recorded,
            requests_requeued=requests_requeued,
            cancellations_signalled=cancellations_signalled,
            unresolved=unresolved,
        )


def _reconciliation_signal_id(candidate: RunReconciliationCandidate) -> str:
    value = f"{candidate.tenant_id}:{candidate.run_id}:cancel".encode()
    return f"reconcile:{hashlib.sha256(value).hexdigest()}"
