"""Opt-in real Temporal test environment execution for the probe workflow."""

import os
import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from packages.application.temporal import (
    RUN_ORCHESTRATOR_TASK_QUEUE,
    PlatformProbeWorkflow,
    platform_probe_activity,
    probe_workflow_id,
)
from packages.contracts.temporal import TemporalWorkerKind, WorkflowProbeInput


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
