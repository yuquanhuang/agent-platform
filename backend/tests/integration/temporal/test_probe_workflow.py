"""Opt-in real Temporal test environment execution for the probe workflow."""

import os
import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    PlatformProbeWorkflow,
    PublishAgentWorkflow,
    platform_probe_activity,
    probe_workflow_id,
)
from packages.contracts.temporal import (
    PublishAgentWorkflowInput,
    PublishReleaseFailureInput,
    TemporalWorkerKind,
    WorkflowProbeInput,
)

FAILED_RELEASE_ID = UUID("99999999-9999-4999-8999-999999999999")
FAILED_RELEASES: list[PublishReleaseFailureInput] = []


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
