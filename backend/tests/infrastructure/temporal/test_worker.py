"""Temporal worker queue and registration boundary tests."""

from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    PlatformProbeWorkflow,
    platform_probe_activity,
)
from packages.contracts.temporal import TemporalWorkerKind
from packages.infrastructure.public import AppSettings
from packages.infrastructure.temporal import probe_worker_definition, temporal_namespace


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
