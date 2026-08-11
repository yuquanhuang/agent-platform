"""Opt-in real Temporal test environment execution for platform workflows."""

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    AgentRunWorkflow,
    PlatformProbeWorkflow,
    PublishAgentWorkflow,
    agent_run_workflow_id,
    platform_probe_activity,
    probe_workflow_id,
)
from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    ApprovalDecidedSignal,
    AssistantTextPart,
    CancelAgentRuntimeInput,
    CancelRunSignal,
    ExecuteAgentRunInput,
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    FinalizeAgentRunResult,
    InspectAgentRuntimeInput,
    ProvisionRunSandboxInput,
    PublishAgentWorkflowInput,
    PublishReleaseFailureInput,
    RecoverAgentRunInput,
    ReleaseRunSandboxInput,
    ReleaseRunSandboxResult,
    RunPreparationResult,
    RunSandboxHandle,
    RunSpecReference,
    RuntimeCancellationResult,
    RuntimeCompletion,
    RuntimeInspection,
    TemporalWorkerKind,
    WorkflowProbeInput,
)

FAILED_RELEASE_ID = UUID("99999999-9999-4999-8999-999999999999")
FAILED_RELEASES: list[PublishReleaseFailureInput] = []
AGENT_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ASSISTANT_MESSAGE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FAILED_AGENT_RUN_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
RECOVERY_AGENT_RUN_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
UNKNOWN_AGENT_RUN_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
_run_execution_gate: asyncio.Event | None = None
RUN_FINALIZATIONS: list[FinalizeAgentRunInput] = []
RUN_EXECUTION_ATTEMPTS: dict[UUID, list[int]] = {}


@activity.defn(name="validate_release_v1")
async def fake_validate_release(input: PublishAgentWorkflowInput) -> None:
    pass


@activity.defn(name="compile_release_bundles_v1")
async def fake_compile_release(input: PublishAgentWorkflowInput) -> None:
    activity.heartbeat("compiled")


@activity.defn(name="scan_release_bundles_v1")
async def fake_scan_release(input: PublishAgentWorkflowInput) -> None:
    if input.release_id == FAILED_RELEASE_ID:
        raise ApplicationError(
            "Bundle scan failed.",
            type="BUNDLE_SCAN_FAILED",
            non_retryable=True,
        )


@activity.defn(name="smoke_test_release_v1")
async def fake_smoke_release(input: PublishAgentWorkflowInput) -> None:
    return None


@activity.defn(name="activate_release_v1")
async def fake_activate_release(input: PublishAgentWorkflowInput) -> None:
    return None


@activity.defn(name="fail_release_v1")
async def fake_fail_release(input: PublishReleaseFailureInput) -> None:
    FAILED_RELEASES.append(input)


@activity.defn(name="prepare_agent_run_v1")
async def fake_prepare_agent_run(
    input: AgentRunWorkflowInput,
) -> RunPreparationResult:
    if input.run_id == FAILED_AGENT_RUN_ID:
        raise ApplicationError(
            "RunSpec compilation failed.",
            type="RUN_SPEC_UNAVAILABLE",
            non_retryable=True,
        )
    return RunPreparationResult(
        run_id=input.run_id,
        execution_attempt=1,
        run_spec=RunSpecReference(
            uri=f"immutable://run-spec/{input.run_id}/1",
            content_hash="sha256:" + "a" * 64,
            size_bytes=1024,
        ),
        timeout_seconds=30,
        runtime_type="agentscope",
    )


@activity.defn(name="provision_run_sandbox_v1")
async def fake_provision_run_sandbox(
    input: ProvisionRunSandboxInput,
) -> RunSandboxHandle:
    return RunSandboxHandle(
        sandbox_instance_id=f"sandbox_{input.run_id}_{input.execution_attempt}",
        lease_id=f"lease_{input.run_id}_{input.execution_attempt}",
        workspace_uri=(
            f"workspace://tenant/{input.tenant_id}/runs/{input.run_id}/"
            f"attempts/{input.execution_attempt}/"
        ),
    )


@activity.defn(name="release_run_sandbox_v1")
async def fake_release_run_sandbox(
    input: ReleaseRunSandboxInput,
) -> ReleaseRunSandboxResult:
    return ReleaseRunSandboxResult(
        sandbox_instance_id=input.sandbox.sandbox_instance_id,
        status="TERMINATED",
    )


@activity.defn(name="execute_agent_run_v1")
async def fake_execute_agent_run(input: ExecuteAgentRunInput) -> RuntimeCompletion:
    assert input.sandbox is not None
    RUN_EXECUTION_ATTEMPTS.setdefault(input.run_id, []).append(input.execution_attempt)
    if (
        input.run_id in {RECOVERY_AGENT_RUN_ID, UNKNOWN_AGENT_RUN_ID}
        and input.execution_attempt == 1
    ):
        raise ApplicationError(
            "Runtime worker was lost.",
            type="RUNTIME_EXECUTION_FAILED",
            non_retryable=True,
        )
    gate = _run_execution_gate
    if gate is not None:
        await gate.wait()
    return RuntimeCompletion(
        status="SUCCEEDED",
        assistant_content_parts=(AssistantTextPart(text="completed"),),
        result_quality="NORMAL",
        runtime_handle_ref=f"runtime://{input.run_id}/{input.execution_attempt}",
    )


@activity.defn(name="finalize_agent_run_v1")
async def fake_finalize_agent_run(
    input: FinalizeAgentRunInput,
) -> FinalizeAgentRunResult:
    RUN_FINALIZATIONS.append(input)
    return FinalizeAgentRunResult(
        run_id=input.run_id,
        status=input.completion.status,
        assistant_message_id=(
            ASSISTANT_MESSAGE_ID if input.completion.status == "SUCCEEDED" else None
        ),
    )


@activity.defn(name="inspect_agent_runtime_v1")
async def fake_inspect_agent_runtime(
    input: InspectAgentRuntimeInput,
) -> RuntimeInspection:
    if input.run_id == RECOVERY_AGENT_RUN_ID:
        return RuntimeInspection(
            run_id=input.run_id,
            execution_attempt=input.execution_attempt,
            status="LOST",
            safe_to_retry=True,
        )
    if input.run_id == UNKNOWN_AGENT_RUN_ID:
        return RuntimeInspection(
            run_id=input.run_id,
            execution_attempt=input.execution_attempt,
            status="UNKNOWN",
        )
    return RuntimeInspection(
        run_id=input.run_id,
        execution_attempt=input.execution_attempt,
        status="RUNNING",
    )


@activity.defn(name="recover_agent_run_v1")
async def fake_recover_agent_run(input: RecoverAgentRunInput) -> RunPreparationResult:
    return RunPreparationResult(
        run_id=input.run_id,
        execution_attempt=input.lost_execution_attempt + 1,
        run_spec=RunSpecReference(
            uri=f"immutable://run-spec/{input.run_id}/{input.lost_execution_attempt + 1}",
            content_hash="sha256:" + "b" * 64,
            size_bytes=1024,
        ),
        timeout_seconds=30,
        runtime_type="agentscope",
    )


@activity.defn(name="cancel_agent_runtime_v1")
async def fake_cancel_agent_runtime(
    input: CancelAgentRuntimeInput,
) -> RuntimeCancellationResult:
    return RuntimeCancellationResult(
        run_id=input.run_id,
        execution_attempt=input.execution_attempt,
        status="CANCELLED",
    )


@activity.defn(name="finalize_agent_run_cancellation_v1")
async def fake_finalize_agent_run_cancellation(
    input: FinalizeAgentRunCancellationInput,
) -> FinalizeAgentRunResult:
    return FinalizeAgentRunResult(
        run_id=input.run_id,
        status=(
            "CANCELLED"
            if input.cancellation.status in {"CANCELLED", "ALREADY_STOPPED"}
            else "FAILED"
        ),
    )


@pytest.mark.asyncio
async def test_probe_workflow_executes_in_temporal_test_environment() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    probe_id = UUID("22222222-2222-4222-8222-222222222222")
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            workflows=[PlatformProbeWorkflow],
            activities=[platform_probe_activity],
        ),
    ):
        result = await environment.client.execute_workflow(
            PlatformProbeWorkflow.run,
            WorkflowProbeInput(
                tenant_id=tenant_id,
                probe_id=probe_id,
                worker_kind=TemporalWorkerKind.RUN,
                request_id="req-temporal-integration",
                trace_id="trace-temporal-integration",
            ),
            id=probe_workflow_id(tenant_id, probe_id),
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
        )

    assert result.status == "SUCCEEDED"
    assert result.worker_kind is TemporalWorkerKind.RUN


@pytest.mark.asyncio
async def test_publish_workflow_executes_release_stages_in_test_environment() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    release_id = UUID("33333333-3333-4333-8333-333333333333")
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    workflow_input = PublishAgentWorkflowInput(
        tenant_id=tenant_id,
        release_id=release_id,
        operation_id=UUID("44444444-4444-4444-8444-444444444444"),
        agent_id=UUID("55555555-5555-4555-8555-555555555555"),
        expected_agent_version=3,
        runtime_targets=["rt_agentscope_default"],
        run_smoke_test=True,
        activate_on_success=False,
        request_id="req-publish-integration",
        trace_id="trace-publish-integration",
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=CONTROL_PLANE_TASK_QUEUE,
            workflows=[PublishAgentWorkflow],
            activities=[
                fake_validate_release,
                fake_compile_release,
                fake_scan_release,
                fake_smoke_release,
                fake_activate_release,
                fake_fail_release,
            ],
        ),
    ):
        result = await environment.client.execute_workflow(
            PublishAgentWorkflow.run,
            workflow_input,
            id=f"publish/{tenant_id}/{release_id}",
            task_queue=CONTROL_PLANE_TASK_QUEUE,
        )

    assert result.status == "SUCCEEDED"
    assert result.release_id == release_id
    assert FAILED_RELEASES == []


@pytest.mark.asyncio
async def test_rollback_workflow_accepts_historical_snapshot_source() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    release_id = UUID("66666666-6666-4666-8666-666666666666")
    snapshot_id = UUID("77777777-7777-4777-8777-777777777777")
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    workflow_input = PublishAgentWorkflowInput(
        tenant_id=tenant_id,
        release_id=release_id,
        operation_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("55555555-5555-4555-8555-555555555555"),
        release_kind="ROLLBACK",
        expected_agent_version=None,
        requested_snapshot_id=snapshot_id,
        runtime_targets=["rt_agentscope_default"],
        run_smoke_test=True,
        activate_on_success=True,
        request_id="req-rollback-integration",
        trace_id="trace-rollback-integration",
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=CONTROL_PLANE_TASK_QUEUE,
            workflows=[PublishAgentWorkflow],
            activities=[
                fake_validate_release,
                fake_compile_release,
                fake_scan_release,
                fake_smoke_release,
                fake_activate_release,
                fake_fail_release,
            ],
        ),
    ):
        result = await environment.client.execute_workflow(
            PublishAgentWorkflow.run,
            workflow_input,
            id=f"publish/{tenant_id}/{release_id}",
            task_queue=CONTROL_PLANE_TASK_QUEUE,
        )

    assert result.status == "SUCCEEDED"
    assert result.release_id == release_id


@pytest.mark.asyncio
async def test_publish_workflow_terminalizes_release_after_stage_failure() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    FAILED_RELEASES.clear()
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    workflow_input = PublishAgentWorkflowInput(
        tenant_id=tenant_id,
        release_id=FAILED_RELEASE_ID,
        operation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        agent_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        expected_agent_version=3,
        runtime_targets=["rt_agentscope_default"],
        run_smoke_test=True,
        activate_on_success=True,
        request_id="req-publish-failure",
        trace_id="trace-publish-failure",
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=CONTROL_PLANE_TASK_QUEUE,
            workflows=[PublishAgentWorkflow],
            activities=[
                fake_validate_release,
                fake_compile_release,
                fake_scan_release,
                fake_smoke_release,
                fake_activate_release,
                fake_fail_release,
            ],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await environment.client.execute_workflow(
                PublishAgentWorkflow.run,
                workflow_input,
                id=f"publish/{tenant_id}/{FAILED_RELEASE_ID}",
                task_queue=CONTROL_PLANE_TASK_QUEUE,
            )

    assert len(FAILED_RELEASES) == 1
    assert FAILED_RELEASES[0].release_id == FAILED_RELEASE_ID
    assert FAILED_RELEASES[0].error_code == "BUNDLE_SCAN_FAILED"


@pytest.mark.asyncio
async def test_agent_run_workflow_query_and_duplicate_cancel_signal() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    global _run_execution_gate
    _run_execution_gate = asyncio.Event()
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    input = AgentRunWorkflowInput(
        tenant_id=tenant_id,
        run_id=AGENT_RUN_ID,
        request_id="req-agent-run-integration",
        trace_id="trace-agent-run-integration",
    )
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                fake_prepare_agent_run,
                fake_provision_run_sandbox,
                fake_execute_agent_run,
                fake_inspect_agent_runtime,
                fake_cancel_agent_runtime,
                fake_recover_agent_run,
                fake_finalize_agent_run,
                fake_finalize_agent_run_cancellation,
                fake_release_run_sandbox,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            AgentRunWorkflow.run,
            input,
            id=agent_run_workflow_id(tenant_id, AGENT_RUN_ID),
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
        )
        state = None
        for _ in range(100):
            state = await handle.query(AgentRunWorkflow.run_state)
            if state.status == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert state is not None and state.status == "RUNNING"
        signal = CancelRunSignal(
            signal_id="cancel-run-integration-1",
            requested_by=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            requested_at=datetime(2026, 8, 8, tzinfo=UTC),
            reason="test intent only",
        )
        await handle.signal(AgentRunWorkflow.cancel_run, signal)
        await handle.signal(AgentRunWorkflow.cancel_run, signal)
        signalled_state = await handle.query(AgentRunWorkflow.run_state)
        assert signalled_state.cancel_requested is True
        assert signalled_state.status == "CANCELLING"
        _run_execution_gate.set()
        result = await handle.result()
        terminal_state = await handle.query(AgentRunWorkflow.run_state)
        history = await handle.fetch_history()

    _run_execution_gate = None
    replay = await Replayer(
        workflows=[AgentRunWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)
    assert result.status == "CANCELLED"
    assert result.assistant_message_id is None
    assert terminal_state.status == "CANCELLED"
    assert terminal_state.cancel_requested is True
    assert replay.replay_failure is None


@pytest.mark.asyncio
async def test_agent_run_workflow_keeps_first_approval_resolution_and_replays() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    global _run_execution_gate
    _run_execution_gate = asyncio.Event()
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    approval_id = UUID("12121212-1212-4212-8212-121212121212")
    rejecting_approval_id = UUID("13131313-1313-4313-8313-131313131313")
    input = AgentRunWorkflowInput(
        tenant_id=tenant_id,
        run_id=AGENT_RUN_ID,
        request_id="req-agent-run-approval-integration",
        trace_id="trace-agent-run-approval-integration",
    )
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                fake_prepare_agent_run,
                fake_provision_run_sandbox,
                fake_execute_agent_run,
                fake_inspect_agent_runtime,
                fake_cancel_agent_runtime,
                fake_recover_agent_run,
                fake_finalize_agent_run,
                fake_finalize_agent_run_cancellation,
                fake_release_run_sandbox,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            AgentRunWorkflow.run,
            input,
            id=agent_run_workflow_id(tenant_id, AGENT_RUN_ID),
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
        )
        state = None
        for _ in range(100):
            state = await handle.query(AgentRunWorkflow.run_state)
            if state.status == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert state is not None and state.status == "RUNNING"

        approved = ApprovalDecidedSignal(
            signal_id="approval-approved-1",
            approval_id=approval_id,
            decision_id=UUID("14141414-1414-4414-8414-141414141414"),
            decision="APPROVED",
            ticket_ref=None,
            decided_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        conflicting_rejection = ApprovalDecidedSignal(
            signal_id="approval-rejected-conflict-1",
            approval_id=approval_id,
            decision_id=UUID("15151515-1515-4515-8515-151515151515"),
            decision="REJECTED",
            ticket_ref=None,
            decided_at=datetime(2026, 8, 10, 0, 0, 1, tzinfo=UTC),
        )
        await handle.signal(AgentRunWorkflow.approval_decided, approved)
        await handle.signal(AgentRunWorkflow.approval_decided, approved)
        await handle.signal(AgentRunWorkflow.approval_decided, conflicting_rejection)
        approved_state = await handle.query(AgentRunWorkflow.run_state)
        assert approved_state.cancel_requested is False

        rejected = ApprovalDecidedSignal(
            signal_id="approval-rejected-2",
            approval_id=rejecting_approval_id,
            decision_id=UUID("16161616-1616-4616-8616-161616161616"),
            decision="REJECTED",
            ticket_ref=None,
            decided_at=datetime(2026, 8, 10, 0, 0, 2, tzinfo=UTC),
        )
        await handle.signal(AgentRunWorkflow.approval_decided, rejected)
        await handle.signal(AgentRunWorkflow.approval_decided, rejected)
        result = await handle.result()
        terminal_state = await handle.query(AgentRunWorkflow.run_state)
        history = await handle.fetch_history()

    _run_execution_gate = None
    replay = await Replayer(
        workflows=[AgentRunWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)
    assert result.status == "CANCELLED"
    assert terminal_state.status == "CANCELLED"
    assert terminal_state.cancel_requested is True
    assert replay.replay_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_id", "expected_status", "expected_attempts"),
    [
        (RECOVERY_AGENT_RUN_ID, "SUCCEEDED", [1, 2]),
        (UNKNOWN_AGENT_RUN_ID, "FAILED", [1]),
    ],
)
async def test_agent_run_workflow_inspects_before_safe_recovery(
    run_id: UUID, expected_status: str, expected_attempts: list[int]
) -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    RUN_EXECUTION_ATTEMPTS.pop(run_id, None)
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    input = AgentRunWorkflowInput(
        tenant_id=tenant_id,
        run_id=run_id,
        request_id=f"req-{run_id}",
        trace_id=f"trace-{run_id}",
    )
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                fake_prepare_agent_run,
                fake_provision_run_sandbox,
                fake_execute_agent_run,
                fake_inspect_agent_runtime,
                fake_cancel_agent_runtime,
                fake_recover_agent_run,
                fake_finalize_agent_run,
                fake_finalize_agent_run_cancellation,
                fake_release_run_sandbox,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            AgentRunWorkflow.run,
            input,
            id=agent_run_workflow_id(tenant_id, run_id),
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
        )
        result = await handle.result()
        history = await handle.fetch_history()

    replay = await Replayer(
        workflows=[AgentRunWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)
    assert result.status == expected_status
    assert RUN_EXECUTION_ATTEMPTS[run_id] == expected_attempts
    assert replay.replay_failure is None


@pytest.mark.asyncio
async def test_agent_run_workflow_terminalizes_prepare_failure() -> None:
    if os.getenv("AP_TEST_TEMPORAL") != "1":
        pytest.skip("AP_TEST_TEMPORAL=1 is required for Temporal integration test")
    RUN_FINALIZATIONS.clear()
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    input = AgentRunWorkflowInput(
        tenant_id=tenant_id,
        run_id=FAILED_AGENT_RUN_ID,
        request_id="req-agent-run-failure",
        trace_id="trace-agent-run-failure",
    )
    download_dir = Path(tempfile.gettempdir()) / "agent-platform-temporal-test"
    download_dir.mkdir(parents=True, exist_ok=True)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=str(download_dir),
        ) as environment,
        Worker(
            environment.client,
            task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                fake_prepare_agent_run,
                fake_provision_run_sandbox,
                fake_execute_agent_run,
                fake_inspect_agent_runtime,
                fake_cancel_agent_runtime,
                fake_recover_agent_run,
                fake_finalize_agent_run,
                fake_finalize_agent_run_cancellation,
                fake_release_run_sandbox,
            ],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await environment.client.execute_workflow(
                AgentRunWorkflow.run,
                input,
                id=agent_run_workflow_id(tenant_id, FAILED_AGENT_RUN_ID),
                task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
            )

    assert len(RUN_FINALIZATIONS) == 1
    assert RUN_FINALIZATIONS[0].execution_attempt == 1
    assert RUN_FINALIZATIONS[0].completion.status == "FAILED"
    assert RUN_FINALIZATIONS[0].completion.error_code == "RUN_SPEC_UNAVAILABLE"
