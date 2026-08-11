"""Deterministic workflows used by the control and Run worker pools."""

import asyncio
from datetime import timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType

from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    AgentRunWorkflowResult,
    AgentRunWorkflowState,
    ApprovalDecidedSignal,
    CancelAgentRuntimeInput,
    CancelRunSignal,
    ExecuteAgentRunInput,
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    FinalizeAgentRunResult,
    InspectAgentRuntimeInput,
    ProvisionRunSandboxInput,
    PublishAgentWorkflowInput,
    PublishAgentWorkflowResult,
    PublishReleaseFailureInput,
    RecoverAgentRunInput,
    ReleaseRunSandboxInput,
    ReleaseRunSandboxResult,
    RunPreparationResult,
    RunSandboxHandle,
    RuntimeCancellationResult,
    RuntimeCompletion,
    RuntimeInspection,
    WorkflowProbeInput,
    WorkflowProbeResult,
)

CONTROL_PLANE_TASK_QUEUE = "control-plane"
RUN_ORCHESTRATOR_TASK_QUEUE = "run-orchestrator"


def probe_workflow_id(tenant_id: UUID, probe_id: UUID) -> str:
    """Build a deterministic tenant-isolated workflow identifier."""

    return f"probe/{tenant_id}/{probe_id}"


def agent_run_workflow_id(tenant_id: UUID, run_id: UUID) -> str:
    """Build the frozen tenant-isolated Agent Run workflow identifier."""

    return f"run/{tenant_id}/{run_id}"


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


@workflow.defn(name="AgentRunWorkflow")
class AgentRunWorkflow:
    """Replay-safe Run lifecycle over small versioned Activity results."""

    def __init__(self) -> None:
        self._state: AgentRunWorkflowState | None = None
        self._processed_cancel_signal_ids: set[str] = set()
        self._processed_approval_signal_ids: set[str] = set()
        self._approval_resolutions: dict[UUID, ApprovalDecidedSignal] = {}
        self._cancel_requested = False
        self._execution_handle = None
        self._cancellation_enabled = False
        self._sandbox_enabled = False

    @workflow.run
    async def run(self, input: AgentRunWorkflowInput) -> AgentRunWorkflowResult:
        self._cancellation_enabled = workflow.patched(
            "ap-e3-005-run-cancel-recovery-v1"
        )
        self._sandbox_enabled = workflow.patched("ap-e5-002-run-sandbox-v1")
        self._state = AgentRunWorkflowState(
            run_id=input.run_id,
            status="CREATED",
            current_activity="prepare_agent_run_v1",
            execution_attempt=0,
            runtime_handle_ref=None,
            runtime_session_id=None,
            sandbox_instance_id=None,
            latest_sequence_no=0,
            cancel_requested=self._cancel_requested,
            waiting_approval_id=None,
        )
        try:
            preparation = await workflow.execute_activity(
                "prepare_agent_run_v1",
                input,
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                result_type=RunPreparationResult,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=15),
                    maximum_attempts=5,
                ),
            )
        except ActivityError as error:
            if self._cancellation_enabled and (
                self._cancel_requested
                or _activity_error_code(error) == "RUN_CANCELLING"
            ):
                return await self._cancel(input)
            finalization = await self._finalize_activity_failure(input, error)
            self._apply_finalization(finalization)
            raise
        if self._cancellation_enabled and self._cancel_requested:
            return await self._cancel(input)
        while True:
            self._update_state(
                status="PREPARING",
                current_activity=(
                    "provision_run_sandbox_v1"
                    if self._sandbox_enabled
                    else "execute_agent_run_v1"
                ),
                execution_attempt=preparation.execution_attempt,
            )
            sandbox: RunSandboxHandle | None = None
            sandbox_execution_attempt = preparation.execution_attempt
            if self._sandbox_enabled:
                try:
                    sandbox = await self._provision_sandbox(input, preparation)
                except ActivityError as error:
                    finalization = await self._finalize_activity_failure(input, error)
                    self._apply_finalization(finalization)
                    raise
            try:
                try:
                    completion = await self._execute(input, preparation, sandbox)
                except asyncio.CancelledError:
                    if self._cancellation_enabled and self._cancel_requested:
                        return await self._cancel(input)
                    raise
                except ActivityError as error:
                    if self._cancellation_enabled and (
                        self._cancel_requested
                        or _activity_error_code(error) == "RUN_CANCELLING"
                    ):
                        return await self._cancel(input)
                    if not self._cancellation_enabled:
                        finalization = await self._finalize_activity_failure(
                            input, error
                        )
                        self._apply_finalization(finalization)
                        raise
                    recovered = await self._recover_or_fail(
                        input, preparation.execution_attempt, error
                    )
                    if isinstance(recovered, RunPreparationResult):
                        preparation = recovered
                        continue
                    return self._result(input, recovered)
                if self._cancellation_enabled and self._cancel_requested:
                    return await self._cancel(input)
                finalization = await self._finalize_completion(
                    input, preparation.execution_attempt, completion
                )
                return self._result(input, finalization)
            finally:
                if sandbox is not None:
                    await self._release_sandbox(
                        input, sandbox_execution_attempt, sandbox
                    )

    @workflow.signal(name="cancel_run")
    async def cancel_run(self, signal: CancelRunSignal) -> None:
        """Record one idempotent intent and interrupt only the active Activity."""

        if signal.signal_id in self._processed_cancel_signal_ids:
            return
        self._processed_cancel_signal_ids.add(signal.signal_id)
        self._cancel_requested = True
        if self._state is not None and self._state.status not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
        }:
            self._state = self._state.model_copy(update={"cancel_requested": True})
            if self._cancellation_enabled and self._execution_handle is not None:
                self._execution_handle.cancel()

    @workflow.signal(name="approval_decided")
    async def approval_decided(self, signal: ApprovalDecidedSignal) -> None:
        """Record one immutable Approval resolution without trusting tool facts."""

        if signal.signal_id in self._processed_approval_signal_ids:
            return
        self._processed_approval_signal_ids.add(signal.signal_id)
        existing = self._approval_resolutions.get(signal.approval_id)
        if existing is None:
            self._approval_resolutions[signal.approval_id] = signal
            if signal.decision in {"REJECTED", "EXPIRED", "CANCELLED"}:
                self._cancel_requested = True
                if self._state is not None:
                    self._state = self._state.model_copy(
                        update={"cancel_requested": True}
                    )
                if self._execution_handle is not None:
                    self._execution_handle.cancel()

    @workflow.query(name="run_state")
    def run_state(self) -> AgentRunWorkflowState:
        if self._state is None:
            raise RuntimeError("AgentRunWorkflow has not initialized its state")
        return self._state

    async def _execute(
        self,
        input: AgentRunWorkflowInput,
        preparation: RunPreparationResult,
        sandbox: RunSandboxHandle | None,
    ) -> RuntimeCompletion:
        execution_input = ExecuteAgentRunInput(
            tenant_id=input.tenant_id,
            run_id=input.run_id,
            execution_attempt=preparation.execution_attempt,
            run_spec=preparation.run_spec,
            timeout_seconds=preparation.timeout_seconds,
            runtime_type=preparation.runtime_type,
            sandbox=sandbox,
            request_id=input.request_id,
            trace_id=input.trace_id,
        )
        self._update_state(status="RUNNING", current_activity="execute_agent_run_v1")
        self._execution_handle = workflow.start_activity(
            "execute_agent_run_v1",
            execution_input,
            start_to_close_timeout=timedelta(seconds=preparation.timeout_seconds + 60),
            schedule_to_close_timeout=timedelta(
                seconds=preparation.timeout_seconds + 360
            ),
            heartbeat_timeout=timedelta(seconds=10),
            result_type=RuntimeCompletion,
            retry_policy=RetryPolicy(maximum_attempts=1),
            cancellation_type=ActivityCancellationType.TRY_CANCEL,
        )
        try:
            return await self._execution_handle
        finally:
            self._execution_handle = None

    async def _provision_sandbox(
        self, input: AgentRunWorkflowInput, preparation: RunPreparationResult
    ) -> RunSandboxHandle:
        handle = await workflow.execute_activity(
            "provision_run_sandbox_v1",
            ProvisionRunSandboxInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=preparation.execution_attempt,
                run_spec=preparation.run_spec,
                runtime_type=preparation.runtime_type,
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(minutes=2),
            schedule_to_close_timeout=timedelta(minutes=5),
            result_type=RunSandboxHandle,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=15),
                maximum_attempts=5,
            ),
        )
        self._update_state(
            sandbox_instance_id=handle.sandbox_instance_id,
            current_activity="execute_agent_run_v1",
        )
        return handle

    async def _release_sandbox(
        self,
        input: AgentRunWorkflowInput,
        execution_attempt: int,
        sandbox: RunSandboxHandle,
    ) -> None:
        self._update_state(current_activity="release_run_sandbox_v1")
        try:
            await workflow.execute_activity(
                "release_run_sandbox_v1",
                ReleaseRunSandboxInput(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    execution_attempt=execution_attempt,
                    sandbox=sandbox,
                    request_id=input.request_id,
                    trace_id=input.trace_id,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                result_type=ReleaseRunSandboxResult,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=15),
                    maximum_attempts=5,
                ),
            )
        except ActivityError:
            # Run finalization remains authoritative; AP-E7-001 reconciles leaked
            # Sandbox instances from terminal Run facts.
            workflow.logger.error(
                "Run Sandbox release exhausted retries run_id=%s sandbox_id=%s",
                input.run_id,
                sandbox.sandbox_instance_id,
            )
        finally:
            self._update_state(current_activity=None)

    async def _recover_or_fail(
        self,
        input: AgentRunWorkflowInput,
        execution_attempt: int,
        error: ActivityError,
    ) -> RunPreparationResult | FinalizeAgentRunResult:
        del error
        self._update_state(current_activity="inspect_agent_runtime_v1")
        inspection = await workflow.execute_activity(
            "inspect_agent_runtime_v1",
            InspectAgentRuntimeInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=execution_attempt,
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=2),
            result_type=RuntimeInspection,
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if inspection.completion is not None:
            return await self._finalize_completion(
                input, inspection.execution_attempt, inspection.completion
            )
        if (
            inspection.status == "LOST"
            and inspection.safe_to_retry
            and execution_attempt < 3
        ):
            self._update_state(current_activity="recover_agent_run_v1")
            return await workflow.execute_activity(
                "recover_agent_run_v1",
                RecoverAgentRunInput(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    lost_execution_attempt=execution_attempt,
                    request_id=input.request_id,
                    trace_id=input.trace_id,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                result_type=RunPreparationResult,
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        return await self._finalize_failure(
            input,
            execution_attempt=execution_attempt,
            error_code="SESSION_NOT_RECOVERABLE",
            error_message=(
                "The Runtime submission state is unknown; the Prompt was not replayed."
                if inspection.status in {"RUNNING", "UNKNOWN"}
                else "The Runtime cannot be recovered safely."
            ),
        )

    async def _cancel(self, input: AgentRunWorkflowInput) -> AgentRunWorkflowResult:
        self._update_state(
            status="CANCELLING",
            current_activity="inspect_agent_runtime_v1",
            cancel_requested=True,
        )
        inspection = await workflow.execute_activity(
            "inspect_agent_runtime_v1",
            InspectAgentRuntimeInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=(
                    self._state.execution_attempt if self._state is not None else 0
                ),
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=2),
            result_type=RuntimeInspection,
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if inspection.status in {"NOT_STARTED", "SUCCEEDED", "FAILED", "LOST"}:
            cancellation = RuntimeCancellationResult(
                run_id=input.run_id,
                execution_attempt=inspection.execution_attempt,
                status="ALREADY_STOPPED",
                runtime_handle_ref=inspection.runtime_handle_ref,
            )
        else:
            self._update_state(current_activity="cancel_agent_runtime_v1")
            cancellation = await workflow.execute_activity(
                "cancel_agent_runtime_v1",
                CancelAgentRuntimeInput(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    execution_attempt=inspection.execution_attempt,
                    request_id=input.request_id,
                    trace_id=input.trace_id,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(minutes=2),
                result_type=RuntimeCancellationResult,
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        self._update_state(current_activity="finalize_agent_run_cancellation_v1")
        finalization = await workflow.execute_activity(
            "finalize_agent_run_cancellation_v1",
            FinalizeAgentRunCancellationInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=cancellation.execution_attempt,
                cancellation=cancellation,
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(minutes=2),
            result_type=FinalizeAgentRunResult,
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return self._result(input, finalization)

    async def _finalize_completion(
        self,
        input: AgentRunWorkflowInput,
        execution_attempt: int,
        completion: RuntimeCompletion,
    ) -> FinalizeAgentRunResult:
        self._update_state(
            current_activity="finalize_agent_run_v1",
            runtime_handle_ref=completion.runtime_handle_ref,
        )
        return await workflow.execute_activity(
            "finalize_agent_run_v1",
            FinalizeAgentRunInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=execution_attempt,
                completion=completion,
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(minutes=2),
            result_type=FinalizeAgentRunResult,
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _finalize_failure(
        self,
        input: AgentRunWorkflowInput,
        *,
        execution_attempt: int,
        error_code: str,
        error_message: str,
    ) -> FinalizeAgentRunResult:
        return await self._finalize_completion(
            input,
            execution_attempt,
            RuntimeCompletion(
                status="FAILED",
                error_code=error_code,
                error_message=error_message,
                retryable=False,
            ),
        )

    async def _finalize_activity_failure(
        self,
        input: AgentRunWorkflowInput,
        error: ActivityError,
    ) -> FinalizeAgentRunResult:
        cause = error.cause
        error_code = (
            cause.type
            if isinstance(cause, ApplicationError) and cause.type
            else "RUN_ACTIVITY_FAILED"
        )
        self._update_state(current_activity="finalize_agent_run_v1")
        return await workflow.execute_activity(
            "finalize_agent_run_v1",
            FinalizeAgentRunInput(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=(
                    self._state.execution_attempt
                    if self._state is not None and self._state.execution_attempt > 0
                    else input.initial_execution_attempt
                ),
                completion=RuntimeCompletion(
                    status="FAILED",
                    error_code=error_code[:64],
                    error_message="The Run workflow stage failed.",
                    retryable=False,
                ),
                request_id=input.request_id,
                trace_id=input.trace_id,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(minutes=2),
            result_type=FinalizeAgentRunResult,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=15),
                maximum_attempts=5,
            ),
        )

    def _apply_finalization(self, result: FinalizeAgentRunResult) -> None:
        self._update_state(status=result.status, current_activity=None)

    def _result(
        self, input: AgentRunWorkflowInput, result: FinalizeAgentRunResult
    ) -> AgentRunWorkflowResult:
        self._apply_finalization(result)
        return AgentRunWorkflowResult(
            run_id=input.run_id,
            status=result.status,
            assistant_message_id=result.assistant_message_id,
        )

    def _update_state(self, **updates: object) -> None:
        if self._state is None:
            raise RuntimeError("AgentRunWorkflow state is unavailable")
        self._state = self._state.model_copy(update=updates)


def _activity_error_code(error: ActivityError) -> str:
    cause = error.cause
    return (
        cause.type
        if isinstance(cause, ApplicationError) and cause.type
        else "RUN_ACTIVITY_FAILED"
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
