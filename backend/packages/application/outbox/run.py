"""Run-specific Outbox start validation and durable mapping handoff."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from packages.application.outbox.dispatcher import (
    PermanentOutboxError,
    RetryableOutboxError,
    WorkflowStartResult,
)
from packages.application.runs import RUN_REQUESTED_EVENT
from packages.application.temporal import agent_run_workflow_id
from packages.contracts.public import TenantContext
from packages.contracts.temporal import RunRequestedPayloadV1
from packages.domain.outbox import OutboxEvent

WorkflowStartOutcome = Literal["STARTED", "ALREADY_EXISTS"]


class RunWorkflowStartStore(Protocol):
    async def record_workflow_start(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        workflow_id: str,
        temporal_run_id: str,
        outcome: WorkflowStartOutcome,
        started_at: datetime,
    ) -> bool: ...


class RunWorkflowStartRecorder:
    """Validate event/result identity before persisting a Run start mapping."""

    def __init__(self, store: RunWorkflowStartStore) -> None:
        self._store = store

    async def record(
        self,
        context: TenantContext,
        event: OutboxEvent,
        result: WorkflowStartResult,
        *,
        now: datetime,
    ) -> None:
        if event.event_type != RUN_REQUESTED_EVENT:
            raise PermanentOutboxError("Run start recorder received another event")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError("unsupported Run payload schema version")
        try:
            payload = RunRequestedPayloadV1.model_validate(event.payload)
        except ValueError as error:
            raise PermanentOutboxError("invalid Run payload") from error
        if payload.tenant_id != event.tenant_id or payload.tenant_id != UUID(
            context.tenant_id
        ):
            raise PermanentOutboxError("Run start tenant identity does not match")
        if payload.run_id != event.aggregate_id:
            raise PermanentOutboxError("Run start aggregate identity does not match")
        expected_workflow_id = agent_run_workflow_id(payload.tenant_id, payload.run_id)
        if result.workflow_id != expected_workflow_id:
            raise PermanentOutboxError("Temporal Workflow identity does not match Run")
        if result.run_id is None:
            raise RetryableOutboxError("Temporal Run ID was not returned")
        outcome: WorkflowStartOutcome = (
            "ALREADY_EXISTS" if result.already_exists else "STARTED"
        )
        await self._store.record_workflow_start(
            context,
            run_id=payload.run_id,
            workflow_id=result.workflow_id,
            temporal_run_id=result.run_id,
            outcome=outcome,
            started_at=now,
        )
