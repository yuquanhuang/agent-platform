"""Deployment-facing API composition for database-backed publication features."""

from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.routes.events import InternalServiceIdentityProvider
from packages.application.event_service import (
    RunEventIngestionService,
    RunEventQueryService,
)
from packages.application.public import (
    DeploymentManagementService,
    MessageHistoryService,
    PublicationQueryService,
    ReleaseManagementService,
    RunManagementService,
    RunWorkflowControl,
    SessionManagementService,
)
from packages.application.temporal import RuntimeTargetReleaseConfig
from packages.contracts.public import IdentityProvider
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import (
    AppSettings,
    SqlAlchemyDeploymentStore,
    SqlAlchemyIamPersistence,
    SqlAlchemyIdentityReader,
    SqlAlchemyMessageHistoryStore,
    SqlAlchemyReleaseStore,
    SqlAlchemyRunEventQueryStore,
    SqlAlchemyRunEventStore,
    SqlAlchemyRunStore,
    SqlAlchemySessionStore,
    SqlAlchemySnapshotCompilationStore,
)


@dataclass(frozen=True, slots=True)
class DatabasePublicationServices:
    """Services required by the Release, history, Diff and Deployment HTTP routes."""

    release: ReleaseManagementService
    deployment: DeploymentManagementService
    query: PublicationQueryService
    session: SessionManagementService
    message: MessageHistoryService
    run: RunManagementService
    event: RunEventIngestionService
    event_query: RunEventQueryService


def build_database_publication_services(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    run_workflow_control: RunWorkflowControl | None = None,
) -> DatabasePublicationServices:
    """Compose real PostgreSQL adapters without resolving deployment Secrets here."""

    access = SqlAlchemyIamPersistence(session_factory)
    snapshot_store = SqlAlchemySnapshotCompilationStore(session_factory)
    return DatabasePublicationServices(
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
        event_query=RunEventQueryService(
            access, SqlAlchemyRunEventQueryStore(session_factory)
        ),
    )


def create_database_publication_app(
    settings: AppSettings,
    *,
    identity_provider: IdentityProvider,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    run_workflow_control: RunWorkflowControl | None = None,
    internal_service_identity_provider: InternalServiceIdentityProvider | None = None,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build an API whose publication routes use real database adapters.

    The deployment layer remains responsible for resolving AP_DATABASE_DSN_REF,
    constructing the Session Factory and loading trusted Runtime Target config.
    """

    services = build_database_publication_services(
        session_factory, runtime_targets, run_workflow_control
    )
    return create_app(
        settings,
        identity_reader=SqlAlchemyIdentityReader(session_factory),
        identity_provider=identity_provider,
        release_service=services.release,
        deployment_service=services.deployment,
        publication_query_service=services.query,
        session_service=services.session,
        message_history_service=services.message,
        run_service=services.run,
        run_event_query_service=services.event_query,
        internal_service_identity_provider=internal_service_identity_provider,
        event_ingestion_service=services.event,
        metrics=metrics,
    )
