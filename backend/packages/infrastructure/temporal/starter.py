"""Outbox-to-Temporal mapping for the AP-E0 connectivity probe only."""

from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from packages.application.outbox import (
    PermanentOutboxError,
    RetryableOutboxError,
    WorkflowStartResult,
)
from packages.application.publishing import RELEASE_REQUESTED_EVENT, publish_workflow_id
from packages.application.runs import RUN_REQUESTED_EVENT
from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    AgentRunWorkflow,
    PlatformProbeWorkflow,
    PublishAgentWorkflow,
    agent_run_workflow_id,
    probe_workflow_id,
)
from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    ProbeRequestedPayloadV1,
    PublishAgentWorkflowInput,
    ReleaseRequestedPayloadV1,
    RunRequestedPayloadV1,
    TemporalWorkerKind,
    WorkflowProbeInput,
)
from packages.domain.outbox import OutboxEvent
from packages.infrastructure.observability import PlatformMetrics, bind_log_context

PROBE_REQUESTED_EVENT_TYPE = "platform_probe_requested.v1"


class TemporalProbeStarter:
    """Strictly map one versioned Outbox event to a deterministic workflow."""

    def __init__(
        self,
        client: Client,
        metrics: PlatformMetrics,
        *,
        rpc_timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._rpc_timeout = rpc_timeout

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        if event.event_type != PROBE_REQUESTED_EVENT_TYPE:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError("unsupported probe payload schema version")
        try:
            payload = ProbeRequestedPayloadV1.model_validate(event.payload)
        except ValueError as error:
            raise PermanentOutboxError("invalid probe payload") from error
        if payload.tenant_id != event.tenant_id:
            raise PermanentOutboxError(
                "probe payload tenant does not match Outbox tenant"
            )
        task_queue = (
            CONTROL_PLANE_TASK_QUEUE
            if payload.worker_kind is TemporalWorkerKind.CONTROL
            else RUN_ORCHESTRATOR_TASK_QUEUE
        )
        workflow_id = probe_workflow_id(payload.tenant_id, payload.probe_id)
        workflow_input = WorkflowProbeInput(**payload.model_dump())
        with bind_log_context(
            request_id=payload.request_id,
            trace_id=payload.trace_id,
            tenant_id=str(payload.tenant_id),
        ):
            try:
                handle = await self._client.start_workflow(
                    PlatformProbeWorkflow.run,
                    workflow_input,
                    id=workflow_id,
                    task_queue=task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError as error:
                self._metrics.temporal_workflow_starts.labels(
                    worker_kind=payload.worker_kind.value,
                    outcome="already_exists",
                ).inc()
                return WorkflowStartResult(
                    workflow_id=error.workflow_id,
                    run_id=error.run_id,
                    already_exists=True,
                )
            except RPCError as error:
                raise RetryableOutboxError("Temporal start RPC failed") from error
        self._metrics.temporal_workflow_starts.labels(
            worker_kind=payload.worker_kind.value,
            outcome="started",
        ).inc()
        return WorkflowStartResult(
            workflow_id=handle.id,
            run_id=handle.result_run_id,
            already_exists=False,
        )


class TemporalReleaseStarter:
    """Map a versioned Release Outbox event to one durable workflow execution."""

    def __init__(
        self,
        client: Client,
        metrics: PlatformMetrics,
        *,
        rpc_timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._rpc_timeout = rpc_timeout

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        if event.event_type != RELEASE_REQUESTED_EVENT:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError("unsupported Release payload schema version")
        try:
            payload = ReleaseRequestedPayloadV1.model_validate(event.payload)
        except ValueError as error:
            raise PermanentOutboxError("invalid Release payload") from error
        if payload.tenant_id != event.tenant_id:
            raise PermanentOutboxError(
                "Release payload tenant does not match Outbox tenant"
            )
        if payload.release_id != event.aggregate_id:
            raise PermanentOutboxError(
                "Release payload identity does not match Outbox aggregate"
            )
        workflow_id = publish_workflow_id(payload.tenant_id, payload.release_id)
        workflow_input = PublishAgentWorkflowInput(**payload.model_dump())
        with bind_log_context(
            request_id=payload.request_id,
            trace_id=payload.trace_id,
            tenant_id=str(payload.tenant_id),
        ):
            try:
                handle = await self._client.start_workflow(
                    PublishAgentWorkflow.run,
                    workflow_input,
                    id=workflow_id,
                    task_queue=CONTROL_PLANE_TASK_QUEUE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError as error:
                self._metrics.temporal_workflow_starts.labels(
                    worker_kind="control",
                    outcome="already_exists",
                ).inc()
                return WorkflowStartResult(
                    workflow_id=error.workflow_id,
                    run_id=error.run_id,
                    already_exists=True,
                )
            except RPCError as error:
                raise RetryableOutboxError("Temporal start RPC failed") from error
        self._metrics.temporal_workflow_starts.labels(
            worker_kind="control",
            outcome="started",
        ).inc()
        return WorkflowStartResult(
            workflow_id=handle.id,
            run_id=handle.result_run_id,
            already_exists=False,
        )


class TemporalRunStarter:
    """Map a versioned Run Outbox event to one durable workflow execution."""

    def __init__(
        self,
        client: Client,
        metrics: PlatformMetrics,
        *,
        rpc_timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._rpc_timeout = rpc_timeout

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        if event.event_type != RUN_REQUESTED_EVENT:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError("unsupported Run payload schema version")
        try:
            payload = RunRequestedPayloadV1.model_validate(event.payload)
        except ValueError as error:
            raise PermanentOutboxError("invalid Run payload") from error
        if payload.tenant_id != event.tenant_id:
            raise PermanentOutboxError(
                "Run payload tenant does not match Outbox tenant"
            )
        if payload.run_id != event.aggregate_id:
            raise PermanentOutboxError(
                "Run payload identity does not match Outbox aggregate"
            )
        workflow_id = agent_run_workflow_id(payload.tenant_id, payload.run_id)
        workflow_input = AgentRunWorkflowInput(**payload.model_dump())
        with bind_log_context(
            request_id=payload.request_id,
            trace_id=payload.trace_id,
            tenant_id=str(payload.tenant_id),
        ):
            try:
                handle = await self._client.start_workflow(
                    AgentRunWorkflow.run,
                    workflow_input,
                    id=workflow_id,
                    task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError as error:
                self._metrics.temporal_workflow_starts.labels(
                    worker_kind="run",
                    outcome="already_exists",
                ).inc()
                return WorkflowStartResult(
                    workflow_id=error.workflow_id,
                    run_id=error.run_id,
                    already_exists=True,
                )
            except RPCError as error:
                raise RetryableOutboxError("Temporal start RPC failed") from error
        self._metrics.temporal_workflow_starts.labels(
            worker_kind="run",
            outcome="started",
        ).inc()
        return WorkflowStartResult(
            workflow_id=handle.id,
            run_id=handle.result_run_id,
            already_exists=False,
        )
