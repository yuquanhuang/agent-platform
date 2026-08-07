"""Deployment-facing API composition for database-backed publication features."""

from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from packages.application.public import (
    DeploymentManagementService,
    PublicationQueryService,
    ReleaseManagementService,
)
from packages.application.temporal import RuntimeTargetReleaseConfig
from packages.contracts.public import IdentityProvider
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import (
    AppSettings,
    SqlAlchemyDeploymentStore,
    SqlAlchemyIamPersistence,
    SqlAlchemyIdentityReader,
    SqlAlchemyReleaseStore,
    SqlAlchemySnapshotCompilationStore,
)


@dataclass(frozen=True, slots=True)
class DatabasePublicationServices:
    """Services required by the Release, history, Diff and Deployment HTTP routes."""

    release: ReleaseManagementService
    deployment: DeploymentManagementService
    query: PublicationQueryService


def build_database_publication_services(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
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
    )


def create_database_publication_app(
    settings: AppSettings,
    *,
    identity_provider: IdentityProvider,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build an API whose publication routes use real database adapters.

    The deployment layer remains responsible for resolving AP_DATABASE_DSN_REF,
    constructing the Session Factory and loading trusted Runtime Target config.
    """

    services = build_database_publication_services(session_factory, runtime_targets)
    return create_app(
        settings,
        identity_reader=SqlAlchemyIdentityReader(session_factory),
        identity_provider=identity_provider,
        release_service=services.release,
        deployment_service=services.deployment,
        publication_query_service=services.query,
        metrics=metrics,
    )
