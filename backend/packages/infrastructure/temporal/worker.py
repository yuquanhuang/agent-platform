"""Independent control/run Temporal worker composition."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from packages.application.temporal import (
    CONTROL_PLANE_TASK_QUEUE,
    RUN_ORCHESTRATOR_TASK_QUEUE,
    AgentRunWorkflow,
    AgentRunWorkflowActivities,
    PlatformProbeWorkflow,
    PublishAgentWorkflow,
    ReleaseWorkflowActivities,
    platform_probe_activity,
)
from packages.contracts.temporal import TemporalWorkerKind
from packages.infrastructure.config import AppSettings
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.temporal.client import connect_temporal_client

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbeWorkerDefinition:
    kind: TemporalWorkerKind
    task_queue: str
    workflows: Sequence[type]
    activities: Sequence[Callable[..., object]]


def release_control_worker_definition(
    publish_activities: ReleaseWorkflowActivities,
) -> ProbeWorkerDefinition:
    """Register the Release workflow only with its explicitly composed Activities."""

    return ProbeWorkerDefinition(
        kind=TemporalWorkerKind.CONTROL,
        task_queue=CONTROL_PLANE_TASK_QUEUE,
        workflows=(PlatformProbeWorkflow, PublishAgentWorkflow),
        activities=(
            platform_probe_activity,
            publish_activities.validate_release,
            publish_activities.compile_release_bundles,
            publish_activities.scan_release_bundles,
            publish_activities.smoke_test_release,
            publish_activities.activate_release,
            publish_activities.fail_release,
        ),
    )


def run_orchestrator_worker_definition(
    run_activities: AgentRunWorkflowActivities,
) -> ProbeWorkerDefinition:
    """Register the Run workflow only with explicitly composed Runtime ports."""

    return ProbeWorkerDefinition(
        kind=TemporalWorkerKind.RUN,
        task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
        workflows=(PlatformProbeWorkflow, AgentRunWorkflow),
        activities=(
            platform_probe_activity,
            run_activities.prepare_agent_run,
            run_activities.execute_agent_run,
            run_activities.inspect_agent_runtime,
            run_activities.cancel_agent_runtime,
            run_activities.recover_agent_run,
            run_activities.finalize_agent_run,
            run_activities.finalize_agent_run_cancellation,
        ),
    )


def probe_worker_definition(kind: TemporalWorkerKind) -> ProbeWorkerDefinition:
    task_queue = (
        CONTROL_PLANE_TASK_QUEUE
        if kind is TemporalWorkerKind.CONTROL
        else RUN_ORCHESTRATOR_TASK_QUEUE
    )
    return ProbeWorkerDefinition(
        kind=kind,
        task_queue=task_queue,
        workflows=(PlatformProbeWorkflow,),
        activities=(platform_probe_activity,),
    )


def create_probe_worker(
    client: Client,
    kind: TemporalWorkerKind,
    *,
    graceful_shutdown_timeout_seconds: float,
) -> Worker:
    definition = probe_worker_definition(kind)
    return Worker(
        client,
        task_queue=definition.task_queue,
        workflows=definition.workflows,
        activities=definition.activities,
        graceful_shutdown_timeout=timedelta(seconds=graceful_shutdown_timeout_seconds),
    )


def create_run_orchestrator_worker(
    client: Client,
    run_activities: AgentRunWorkflowActivities,
    *,
    graceful_shutdown_timeout_seconds: float,
) -> Worker:
    definition = run_orchestrator_worker_definition(run_activities)
    return Worker(
        client,
        task_queue=definition.task_queue,
        workflows=definition.workflows,
        activities=definition.activities,
        graceful_shutdown_timeout=timedelta(seconds=graceful_shutdown_timeout_seconds),
    )


async def run_probe_worker_process(
    settings: AppSettings,
    kind: TemporalWorkerKind,
    metrics: PlatformMetrics,
) -> None:
    """Connect and run exactly one fixed worker pool until shutdown."""

    client = await connect_temporal_client(settings)
    worker = create_probe_worker(
        client,
        kind,
        graceful_shutdown_timeout_seconds=settings.worker_shutdown_grace_seconds,
    )
    metrics.process_up.labels(process=settings.service_name).set(1)
    LOGGER.info("Temporal worker started task_queue=%s", worker.task_queue)
    try:
        await worker.run()
    finally:
        metrics.process_up.labels(process=settings.service_name).set(0)


async def run_agent_workflow_worker_process(
    settings: AppSettings,
    metrics: PlatformMetrics,
    run_activities: AgentRunWorkflowActivities,
) -> None:
    """Run the composed Agent workflow pool; missing adapters fail before startup."""

    client = await connect_temporal_client(settings)
    worker = create_run_orchestrator_worker(
        client,
        run_activities,
        graceful_shutdown_timeout_seconds=settings.worker_shutdown_grace_seconds,
    )
    metrics.process_up.labels(process=settings.service_name).set(1)
    LOGGER.info("Temporal Agent Run worker started task_queue=%s", worker.task_queue)
    try:
        await worker.run()
    finally:
        metrics.process_up.labels(process=settings.service_name).set(0)
