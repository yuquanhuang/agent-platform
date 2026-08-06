"""Minimal deterministic workflow used by both orchestration worker pools."""

from datetime import timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from packages.contracts.temporal import WorkflowProbeInput, WorkflowProbeResult

CONTROL_PLANE_TASK_QUEUE = "control-plane"
RUN_ORCHESTRATOR_TASK_QUEUE = "run-orchestrator"


def probe_workflow_id(tenant_id: UUID, probe_id: UUID) -> str:
    """Build a deterministic tenant-isolated workflow identifier."""

    return f"probe/{tenant_id}/{probe_id}"


@activity.defn(name="platform_probe_activity_v1")
async def platform_probe_activity(input: WorkflowProbeInput) -> WorkflowProbeResult:
    """Verify Activity execution without touching platform business state."""

    workflow_id = activity.info().workflow_id
    if workflow_id is None:
        raise RuntimeError("Temporal Activity did not provide a workflow_id")
    return WorkflowProbeResult(
        workflow_id=workflow_id,
        worker_kind=input.worker_kind,
    )


@workflow.defn(name="PlatformProbeWorkflow")
class PlatformProbeWorkflow:
    """Replay-safe probe; all observable work remains in the Activity."""

    @workflow.run
    async def run(self, input: WorkflowProbeInput) -> WorkflowProbeResult:
        return await workflow.execute_activity(
            platform_probe_activity,
            input,
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=15),
                maximum_attempts=5,
            ),
        )
