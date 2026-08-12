"""Deployment-facing API composition for database-backed publication features."""

from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

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
from packages.application.policy import RunCapacityPolicy
from packages.application.public import (
    ApprovalManagementService,
    ApprovalWorkflowControl,
    ArtifactDownloadGatewayService,
    ArtifactDownloadObjectReader,
    ArtifactDownloadRevocationSource,
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
    QuotaPolicyManagementService,
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
    RandomArtifactDownloadCredentialIssuer,
    SqlAlchemyAgentResourceReferenceProvider,
    SqlAlchemyApprovalStore,
    SqlAlchemyArtifactStore,
    SqlAlchemyAuditQueryStore,
    SqlAlchemyDeploymentStore,
    SqlAlchemyIamPersistence,
    SqlAlchemyIdentityReader,
    SqlAlchemyMcpDiscoveryStore,
    SqlAlchemyMessageHistoryStore,
    SqlAlchemyQuotaPolicyStore,
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
    quota_policy: QuotaPolicyManagementService
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
    artifact_gateway: ArtifactDownloadGatewayService | None
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
    artifact_download_reader: ArtifactDownloadObjectReader | None = None,
    artifact_download_gateway_subject_id: UUID | None = None,
    artifact_download_revocation_source: ArtifactDownloadRevocationSource | None = None,
) -> DatabasePublicationServices:
    """Compose real PostgreSQL adapters without resolving deployment Secrets here."""

    access = SqlAlchemyIamPersistence(session_factory)
    snapshot_store = SqlAlchemySnapshotCompilationStore(session_factory)
    event_query = RunEventQueryService(
        access, SqlAlchemyRunEventQueryStore(session_factory)
    )
    resolved_settings = settings or AppSettings()
    deployment_capacity_policy = _run_capacity_policy(resolved_settings)
    if (
        artifact_object_store is not None
        and not resolved_settings.artifact_public_origins
    ):
        raise ValueError(
            "AP_ARTIFACT_PUBLIC_ORIGINS is required when Artifact storage is enabled"
        )
    if artifact_object_store is not None and (
        artifact_download_reader is None or artifact_download_gateway_subject_id is None
    ):
        raise ValueError(
            "Artifact Download Gateway reader and service subject are required "
            "when Artifact storage is enabled"
        )
    if artifact_object_store is None and (
        artifact_download_reader is not None
        or artifact_download_gateway_subject_id is not None
        or artifact_download_revocation_source is not None
    ):
        raise ValueError(
            "Artifact storage is required when the Download Gateway is configured"
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
    approval_store = SqlAlchemyApprovalStore(session_factory)
    artifact_store = SqlAlchemyArtifactStore(session_factory)
    artifact_credentials = RandomArtifactDownloadCredentialIssuer()
    return DatabasePublicationServices(
        iam=IamManagementService(access),
        quota_policy=QuotaPolicyManagementService(
            access,
            SqlAlchemyQuotaPolicyStore(session_factory),
            deployment_capacity_policy,
        ),
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
            access,
            SqlAlchemyRunStore(
                session_factory,
                run_capacity_policy=deployment_capacity_policy,
            ),
            run_workflow_control,
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
                artifact_store,
                artifact_object_store,
                ArtifactGrantUrlPolicy(
                    frozenset(resolved_settings.artifact_public_origins)
                ),
                artifact_credentials,
                str(resolved_settings.public_base_url),
            )
            if artifact_object_store is not None
            else None
        ),
        artifact_gateway=(
            ArtifactDownloadGatewayService(
                artifact_store,
                artifact_download_reader,
                service_subject_id=artifact_download_gateway_subject_id,
                revocation_source=artifact_download_revocation_source,
                revocation_check_interval=timedelta(
                    seconds=(
                        resolved_settings.artifact_download_revocation_check_interval_seconds
                    )
                ),
                max_stream_duration=timedelta(
                    seconds=resolved_settings.artifact_download_max_duration_seconds
                ),
            )
            if artifact_download_reader is not None
            and artifact_download_gateway_subject_id is not None
            else None
        ),
        approval=ApprovalManagementService(
            access,
            approval_store,
            (
                cast(ApprovalWorkflowControl, run_workflow_control)
                if run_workflow_control is not None
                else None
            ),
            execution_ticket_issuer,
            approval_store,
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


def _run_capacity_policy(settings: AppSettings) -> RunCapacityPolicy:
    values = {
        "AP_RUN_MAX_NONTERMINAL_PER_TENANT": settings.run_max_nonterminal_per_tenant,
        "AP_RUN_MAX_NONTERMINAL_PER_USER": settings.run_max_nonterminal_per_user,
        "AP_RUN_MAX_NONTERMINAL_PER_AGENT": settings.run_max_nonterminal_per_agent,
        "AP_RUN_MAX_NONTERMINAL_AGENTSCOPE": settings.run_max_nonterminal_agentscope,
        "AP_RUN_MAX_NONTERMINAL_CODEX": settings.run_max_nonterminal_codex,
    }
    if settings.env.value in {"staging", "production"}:
        missing = [name for name, value in values.items() if value is None]
        if missing:
            raise ValueError("Run capacity admission requires: " + ", ".join(missing))
    return RunCapacityPolicy(
        max_nonterminal_runs_per_tenant=settings.run_max_nonterminal_per_tenant,
        max_nonterminal_runs_per_user=settings.run_max_nonterminal_per_user,
        max_nonterminal_runs_per_agent=settings.run_max_nonterminal_per_agent,
        max_nonterminal_agentscope_runs=settings.run_max_nonterminal_agentscope,
        max_nonterminal_codex_runs=settings.run_max_nonterminal_codex,
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
    artifact_download_reader: ArtifactDownloadObjectReader | None = None,
    artifact_download_gateway_subject_id: UUID | None = None,
    artifact_download_revocation_source: ArtifactDownloadRevocationSource | None = None,
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
        run_workflow_control=run_workflow_control,
        event_notification_source=event_notification_source,
        settings=settings,
        artifact_object_store=artifact_object_store,
        trusted_skill_artifact_reader=trusted_skill_artifact_reader,
        skill_scanner=skill_scanner,
        execution_ticket_issuer=execution_ticket_issuer,
        artifact_download_reader=artifact_download_reader,
        artifact_download_gateway_subject_id=artifact_download_gateway_subject_id,
        artifact_download_revocation_source=artifact_download_revocation_source,
    )
    return create_app(
        settings,
        identity_reader=SqlAlchemyIdentityReader(session_factory),
        identity_provider=identity_provider,
        iam_service=services.iam,
        quota_policy_service=services.quota_policy,
        release_service=services.release,
        deployment_service=services.deployment,
        publication_query_service=services.query,
        session_service=services.session,
        message_history_service=services.message,
        run_service=services.run,
        run_event_query_service=services.event_query,
        run_event_stream_service=services.event_stream,
        artifact_service=services.artifact,
        artifact_download_gateway=services.artifact_gateway,
        approval_service=services.approval,
        audit_service=services.audit,
        skill_service=services.skill,
        mcp_service=services.mcp,
        internal_service_identity_provider=internal_service_identity_provider,
        event_ingestion_service=services.event,
        metrics=metrics,
    )
