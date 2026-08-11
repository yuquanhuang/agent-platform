"""Composition for the durable Outbox-to-Temporal routing boundary."""

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ARTIFACT_SCAN_REQUESTED_EVENT,
    ArchiveAwareArtifactSecurityScanner,
    ArtifactDeleteProcessor,
    ArtifactDeletionObjectStore,
    ArtifactLifecycleDispatcher,
    ArtifactQuarantineContentReader,
    ArtifactScanDispatcher,
    ArtifactScanProcessor,
    ArtifactSecurityScanner,
    ArtifactTrustedObjectPublisher,
)
from packages.application.event_service import (
    RUN_EVENTS_APPENDED_EVENT,
    RunEventNotificationDispatcher,
)
from packages.application.outbox import (
    OutboxDispatcher,
    OutboxEventRouter,
    OutboxStartResultRouter,
    RunWorkflowStartRecorder,
    WorkflowStarter,
)
from packages.application.outbox.dispatcher import WorkflowStartResultRecorder
from packages.application.public import (
    MCP_CAPABILITY_DISCOVERY_EVENT,
    McpCapabilityDiscoverer,
    McpCapabilityDiscoveryHandler,
)
from packages.application.publishing import RELEASE_REQUESTED_EVENT
from packages.application.runs import RUN_REQUESTED_EVENT
from packages.infrastructure.config import AppSettings
from packages.infrastructure.database.artifacts import SqlAlchemyArtifactStore
from packages.infrastructure.database.mcp import SqlAlchemyMcpDiscoveryStore
from packages.infrastructure.database.outbox import SqlAlchemyOutboxStore
from packages.infrastructure.database.runs import SqlAlchemyRunStore
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.redis.events import (
    AsyncRedisClient,
    RedisRunEventNotificationPublisher,
)
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


def build_run_event_notification_dispatcher(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: AsyncRedisClient,
) -> RunEventNotificationDispatcher:
    """Build the isolated RunEvent Outbox-to-Redis dispatcher."""

    return RunEventNotificationDispatcher(
        SqlAlchemyOutboxStore(
            session_factory,
            event_types=frozenset({RUN_EVENTS_APPENDED_EVENT}),
        ),
        RedisRunEventNotificationPublisher(redis),
        batch_size=settings.outbox_batch_size,
        max_attempts=settings.outbox_max_attempts,
    )


def build_artifact_scan_dispatcher(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    scanner: ArtifactSecurityScanner,
    quarantine_reader: ArtifactQuarantineContentReader,
    publisher: ArtifactTrustedObjectPublisher,
) -> ArtifactScanDispatcher:
    """Build the isolated Artifact quarantine scan dispatcher."""

    artifact_store = SqlAlchemyArtifactStore(session_factory)
    return ArtifactScanDispatcher(
        SqlAlchemyOutboxStore(
            session_factory,
            event_types=frozenset({ARTIFACT_SCAN_REQUESTED_EVENT}),
        ),
        ArtifactScanProcessor(
            artifact_store,
            ArchiveAwareArtifactSecurityScanner(scanner, quarantine_reader),
            publisher,
        ),
        batch_size=settings.outbox_batch_size,
        max_attempts=settings.outbox_max_attempts,
    )


def build_artifact_lifecycle_dispatcher(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    objects: ArtifactDeletionObjectStore,
) -> ArtifactLifecycleDispatcher:
    """Build expiry sweeping and isolated Artifact deletion dispatch."""

    artifact_store = SqlAlchemyArtifactStore(session_factory)
    return ArtifactLifecycleDispatcher(
        artifact_store,
        SqlAlchemyOutboxStore(
            session_factory,
            event_types=frozenset({ARTIFACT_DELETE_REQUESTED_EVENT}),
        ),
        ArtifactDeleteProcessor(artifact_store, objects),
        batch_size=settings.outbox_batch_size,
        max_attempts=settings.outbox_max_attempts,
    )


def build_mcp_discovery_dispatcher(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    discoverer: McpCapabilityDiscoverer,
) -> OutboxDispatcher:
    """Build isolated MCP discovery routing with an explicitly injected Gateway."""

    handler = McpCapabilityDiscoveryHandler(
        discoverer=discoverer,
        store=SqlAlchemyMcpDiscoveryStore(session_factory),
    )
    return OutboxDispatcher(
        SqlAlchemyOutboxStore(
            session_factory,
            event_types=frozenset({MCP_CAPABILITY_DISCOVERY_EVENT}),
        ),
        OutboxEventRouter({MCP_CAPABILITY_DISCOVERY_EVENT: handler}),
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
