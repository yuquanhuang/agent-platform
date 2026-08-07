"""Minimal deterministic workflow used by both orchestration worker pools."""

from datetime import timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from packages.contracts.temporal import (
    PublishAgentWorkflowInput,
    PublishAgentWorkflowResult,
    PublishReleaseFailureInput,
    WorkflowProbeInput,
    WorkflowProbeResult,
)

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


@workflow.defn(name="PublishAgentWorkflow")
class PublishAgentWorkflow:
    """Replay-safe orchestration; all business state changes live in Activities."""

    @workflow.run
    async def run(self, input: PublishAgentWorkflowInput) -> PublishAgentWorkflowResult:
        try:
            await workflow.execute_activity(
                "validate_release_v1",
                input,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "compile_release_bundles_v1",
                input,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            await workflow.execute_activity(
                "scan_release_bundles_v1",
                input,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            if input.run_smoke_test:
                await workflow.execute_activity(
                    "smoke_test_release_v1",
                    input,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            await workflow.execute_activity(
                "activate_release_v1",
                input,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as error:
            cause = error.cause
            error_code = (
                cause.type
                if isinstance(cause, ApplicationError) and cause.type
                else "RELEASE_STAGE_FAILED"
            )
            await workflow.execute_activity(
                "fail_release_v1",
                PublishReleaseFailureInput(
                    **input.model_dump(),
                    error_code=error_code[:128],
                    error_message="The Release workflow stage failed.",
                ),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            raise
        return PublishAgentWorkflowResult(release_id=input.release_id)
