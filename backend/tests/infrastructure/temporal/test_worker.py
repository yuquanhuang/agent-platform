"""Temporal worker queue and registration boundary tests."""

from typing import cast

from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    PlatformProbeWorkflow,
    PublishAgentWorkflow,
    ReleaseWorkflowActivities,
    platform_probe_activity,
)
from packages.contracts.temporal import TemporalWorkerKind
from packages.infrastructure.public import AppSettings
from packages.infrastructure.temporal import (
    probe_worker_definition,
    release_control_worker_definition,
    temporal_namespace,
)


class FakePublishActivities:
    async def validate_release(self, input: object) -> None:
        return None

    async def compile_release_bundles(self, input: object) -> None:
        return None

    async def scan_release_bundles(self, input: object) -> None:
        return None

    async def smoke_test_release(self, input: object) -> None:
        return None

    async def activate_release(self, input: object) -> None:
        return None

    async def fail_release(self, input: object) -> None:
        return None


def test_control_and_run_workers_use_independent_fixed_queues() -> None:
    control = probe_worker_definition(TemporalWorkerKind.CONTROL)
    run = probe_worker_definition(TemporalWorkerKind.RUN)

    assert control.task_queue == CONTROL_PLANE_TASK_QUEUE
    assert run.task_queue == RUN_ORCHESTRATOR_TASK_QUEUE
    assert control.task_queue != run.task_queue
    assert control.workflows == run.workflows == (PlatformProbeWorkflow,)
    assert control.activities == run.activities == (platform_probe_activity,)


def test_temporal_namespace_defaults_to_environment_baseline() -> None:
    settings = AppSettings.model_validate({"env": "test"})

    assert temporal_namespace(settings) == "agent-platform-test"


def test_release_control_worker_registers_publish_workflow_and_all_activities() -> None:
    definition = release_control_worker_definition(
        cast(ReleaseWorkflowActivities, FakePublishActivities())
    )

    assert definition.task_queue == CONTROL_PLANE_TASK_QUEUE
    assert definition.workflows == (PlatformProbeWorkflow, PublishAgentWorkflow)
    assert len(definition.activities) == 7
