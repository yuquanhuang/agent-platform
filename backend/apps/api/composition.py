"""Deployment-facing API composition for database-backed publication features."""

from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.routes.events import InternalServiceIdentityProvider
from packages.application.artifacts import ArtifactGrantUrlPolicy
from packages.application.event_service import (
    RunEventIngestionService,
    RunEventNotificationSource,
    RunEventQueryService,
    RunEventStreamService,
)
from packages.application.public import (
    ApprovalManagementService,
    ApprovalWorkflowControl,
    ArtifactManagementService,
    ArtifactObjectStore,
    AuditManagementService,
    BaselineSkillSupplyChainScanner,
    CompositeResourceReferenceReader,
    DeploymentManagementService,
    ExecutionTicketIssuer,
    IamManagementService,
    McpManagementService,
    McpRegistry,
    MessageHistoryService,
    PublicationQueryService,
    ReleaseManagementService,
    RunManagementService,
    RunWorkflowControl,
    SessionManagementService,
    SkillManagementService,
    SkillRegistry,
    SkillSupplyChainScanner,
    TrustedSkillArtifactContentReader,
)
from packages.application.temporal import RuntimeTargetReleaseConfig
from packages.contracts.public import IdentityProvider
from packages.infrastructure.database.public import SqlAlchemyResourceRegistry
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import (
    AppSettings,
    SqlAlchemyAgentResourceReferenceProvider,
    SqlAlchemyApprovalStore,
    SqlAlchemyArtifactStore,
    SqlAlchemyAuditQueryStore,
    SqlAlchemyDeploymentStore,
    SqlAlchemyIamPersistence,
    SqlAlchemyIdentityReader,
    SqlAlchemyMcpDiscoveryStore,
    SqlAlchemyMessageHistoryStore,
    SqlAlchemyReleaseStore,
    SqlAlchemyRunEventQueryStore,
    SqlAlchemyRunEventStore,
    SqlAlchemyRunStore,
    SqlAlchemySessionStore,
    SqlAlchemySkillArtifactReader,
    SqlAlchemySkillScanStore,
    SqlAlchemySnapshotCompilationStore,
)


@dataclass(frozen=True, slots=True)
class DatabasePublicationServices:
    """Services required by the Release, history, Diff and Deployment HTTP routes."""

    iam: IamManagementService
    release: ReleaseManagementService
    deployment: DeploymentManagementService
    query: PublicationQueryService
    session: SessionManagementService
    message: MessageHistoryService
    run: RunManagementService
    event: RunEventIngestionService
    event_query: RunEventQueryService
    event_stream: RunEventStreamService
    artifact: ArtifactManagementService | None
    approval: ApprovalManagementService
    audit: AuditManagementService
    skill: SkillManagementService | None
    mcp: McpManagementService


def build_database_publication_services(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    run_workflow_control: RunWorkflowControl | None = None,
    event_notification_source: RunEventNotificationSource | None = None,
    settings: AppSettings | None = None,
    artifact_object_store: ArtifactObjectStore | None = None,
    trusted_skill_artifact_reader: TrustedSkillArtifactContentReader | None = None,
    skill_scanner: SkillSupplyChainScanner | None = None,
    execution_ticket_issuer: ExecutionTicketIssuer | None = None,
) -> DatabasePublicationServices:
    """Compose real PostgreSQL adapters without resolving deployment Secrets here."""

    access = SqlAlchemyIamPersistence(session_factory)
    snapshot_store = SqlAlchemySnapshotCompilationStore(session_factory)
    event_query = RunEventQueryService(
        access, SqlAlchemyRunEventQueryStore(session_factory)
    )
    resolved_settings = settings or AppSettings()
    if (
        artifact_object_store is not None
        and not resolved_settings.artifact_public_origins
    ):
        raise ValueError(
            "AP_ARTIFACT_PUBLIC_ORIGINS is required when Artifact storage is enabled"
        )
    if skill_scanner is not None and trusted_skill_artifact_reader is None:
        raise ValueError(
            "Trusted Skill Artifact content reader is required when a Skill scanner is configured"
        )
    skill_service = None
    registry = SqlAlchemyResourceRegistry(session_factory)
    references = CompositeResourceReferenceReader(
        [SqlAlchemyAgentResourceReferenceProvider(session_factory)]
    )
    if trusted_skill_artifact_reader is not None:
        skill_service = SkillManagementService(
            access,
            cast(SkillRegistry, registry),
            references,
            SqlAlchemySkillArtifactReader(
                session_factory, trusted_skill_artifact_reader
            ),
            skill_scanner or BaselineSkillSupplyChainScanner(),
            SqlAlchemySkillScanStore(session_factory),
        )
    return DatabasePublicationServices(
        iam=IamManagementService(access),
        release=ReleaseManagementService(
            access, SqlAlchemyReleaseStore(session_factory)
        ),
        deployment=DeploymentManagementService(
            access, SqlAlchemyDeploymentStore(session_factory, runtime_targets)
        ),
        query=PublicationQueryService(access, snapshot_store),
        session=SessionManagementService(
            access, SqlAlchemySessionStore(session_factory)
        ),
        message=MessageHistoryService(
            access, SqlAlchemyMessageHistoryStore(session_factory)
        ),
        run=RunManagementService(
            access, SqlAlchemyRunStore(session_factory), run_workflow_control
        ),
        event=RunEventIngestionService(SqlAlchemyRunEventStore(session_factory)),
        event_query=event_query,
        event_stream=RunEventStreamService(
            event_query,
            event_notification_source,
            page_size=resolved_settings.sse_page_size,
            heartbeat_seconds=resolved_settings.sse_heartbeat_seconds,
            poll_interval_seconds=resolved_settings.sse_poll_interval_seconds,
        ),
        artifact=(
            ArtifactManagementService(
                access,
                SqlAlchemyArtifactStore(session_factory),
                artifact_object_store,
                ArtifactGrantUrlPolicy(
                    frozenset(resolved_settings.artifact_public_origins)
                ),
            )
            if artifact_object_store is not None
            else None
        ),
        approval=ApprovalManagementService(
            access,
            SqlAlchemyApprovalStore(session_factory),
            (
                cast(ApprovalWorkflowControl, run_workflow_control)
                if run_workflow_control is not None
                else None
            ),
            execution_ticket_issuer,
        ),
        audit=AuditManagementService(
            access, SqlAlchemyAuditQueryStore(session_factory)
        ),
        skill=skill_service,
        mcp=McpManagementService(
            access,
            cast(McpRegistry, registry),
            references,
            SqlAlchemyMcpDiscoveryStore(session_factory),
        ),
    )


def create_database_publication_app(
    settings: AppSettings,
    *,
    identity_provider: IdentityProvider,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    run_workflow_control: RunWorkflowControl | None = None,
    event_notification_source: RunEventNotificationSource | None = None,
    artifact_object_store: ArtifactObjectStore | None = None,
    trusted_skill_artifact_reader: TrustedSkillArtifactContentReader | None = None,
    skill_scanner: SkillSupplyChainScanner | None = None,
    execution_ticket_issuer: ExecutionTicketIssuer | None = None,
    internal_service_identity_provider: InternalServiceIdentityProvider | None = None,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build an API whose publication routes use real database adapters.

    The deployment layer remains responsible for resolving AP_DATABASE_DSN_REF,
    constructing the Session Factory and loading trusted Runtime Target config.
    """

    services = build_database_publication_services(
        session_factory,
        runtime_targets,
        run_workflow_control,
        event_notification_source,
        settings,
        artifact_object_store,
        trusted_skill_artifact_reader,
        skill_scanner,
        execution_ticket_issuer,
    )
    return create_app(
        settings,
        identity_reader=SqlAlchemyIdentityReader(session_factory),
        identity_provider=identity_provider,
        iam_service=services.iam,
        release_service=services.release,
        deployment_service=services.deployment,
        publication_query_service=services.query,
        session_service=services.session,
        message_history_service=services.message,
        run_service=services.run,
        run_event_query_service=services.event_query,
        run_event_stream_service=services.event_stream,
        artifact_service=services.artifact,
        approval_service=services.approval,
        audit_service=services.audit,
        skill_service=services.skill,
        mcp_service=services.mcp,
        internal_service_identity_provider=internal_service_identity_provider,
        event_ingestion_service=services.event,
        metrics=metrics,
    )
