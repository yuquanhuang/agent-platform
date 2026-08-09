"""Composition for the durable Outbox-to-Temporal routing boundary."""

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from packages.application.outbox import (
    OutboxDispatcher,
    OutboxEventRouter,
    OutboxStartResultRouter,
    RunWorkflowStartRecorder,
    WorkflowStarter,
)
from packages.application.outbox.dispatcher import WorkflowStartResultRecorder
from packages.application.publishing import RELEASE_REQUESTED_EVENT
from packages.application.runs import RUN_REQUESTED_EVENT
from packages.infrastructure.config import AppSettings
from packages.infrastructure.database.outbox import SqlAlchemyOutboxStore
from packages.infrastructure.database.runs import SqlAlchemyRunStore
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.temporal import (
    PROBE_REQUESTED_EVENT_TYPE,
    TemporalProbeStarter,
    TemporalReleaseStarter,
    TemporalRunStarter,
)


def build_temporal_outbox_dispatcher(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    temporal_client: Client,
    metrics: PlatformMetrics,
    additional_routes: Mapping[str, WorkflowStarter] | None = None,
    additional_result_recorders: (
        Mapping[str, WorkflowStartResultRecorder] | None
    ) = None,
) -> OutboxDispatcher:
    """Build the strict production Router without resolving deployment secrets."""

    routes: dict[str, WorkflowStarter] = {
        PROBE_REQUESTED_EVENT_TYPE: TemporalProbeStarter(temporal_client, metrics),
        RELEASE_REQUESTED_EVENT: TemporalReleaseStarter(temporal_client, metrics),
        RUN_REQUESTED_EVENT: TemporalRunStarter(temporal_client, metrics),
    }
    _merge_without_override(routes, additional_routes or {})
    run_store = SqlAlchemyRunStore(session_factory)
    result_recorders: dict[str, WorkflowStartResultRecorder] = {
        RUN_REQUESTED_EVENT: RunWorkflowStartRecorder(run_store)
    }
    _merge_without_override(result_recorders, additional_result_recorders or {})
    return OutboxDispatcher(
        SqlAlchemyOutboxStore(session_factory, event_types=frozenset(routes)),
        OutboxEventRouter(routes),
        result_recorder=OutboxStartResultRouter(result_recorders),
        batch_size=settings.outbox_batch_size,
        max_attempts=settings.outbox_max_attempts,
    )


def _merge_without_override[T](
    target: dict[str, T], additions: Mapping[str, T]
) -> None:
    overlap = target.keys() & additions.keys()
    if overlap:
        raise ValueError(f"Outbox routes cannot be overridden: {sorted(overlap)}")
    target.update(additions)
