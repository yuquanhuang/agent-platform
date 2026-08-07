"""Deployment-facing API composition tests."""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition import build_database_publication_services
from packages.application.public import (
    DeploymentManagementService,
    PublicationQueryService,
    ReleaseManagementService,
)


def test_database_publication_composition_builds_all_http_services() -> None:
    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()), {}
    )

    assert isinstance(services.release, ReleaseManagementService)
    assert isinstance(services.deployment, DeploymentManagementService)
    assert isinstance(services.query, PublicationQueryService)
