"""Deployment-facing API composition tests."""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition import build_database_publication_services
from packages.application.public import (
    ArtifactObjectStore,
    AuditManagementService,
    DeploymentManagementService,
    IamManagementService,
    McpManagementService,
    MessageHistoryService,
    PublicationQueryService,
    ReleaseManagementService,
    RunEventIngestionService,
    RunEventQueryService,
    RunEventStreamService,
    RunManagementService,
    SessionManagementService,
    SkillManagementService,
    SkillSupplyChainScanner,
    TrustedSkillArtifactContentReader,
)
from packages.infrastructure.public import AppSettings


def test_database_publication_composition_builds_all_http_services() -> None:
    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()), {}
    )

    assert isinstance(services.release, ReleaseManagementService)
    assert isinstance(services.iam, IamManagementService)
    assert isinstance(services.deployment, DeploymentManagementService)
    assert isinstance(services.query, PublicationQueryService)
    assert isinstance(services.session, SessionManagementService)
    assert isinstance(services.message, MessageHistoryService)
    assert isinstance(services.run, RunManagementService)
    assert isinstance(services.event, RunEventIngestionService)
    assert isinstance(services.event_query, RunEventQueryService)
    assert isinstance(services.event_stream, RunEventStreamService)
    assert isinstance(services.audit, AuditManagementService)
    assert isinstance(services.mcp, McpManagementService)
    assert services.skill is None


def test_artifact_composition_requires_explicit_signed_url_origins() -> None:
    with pytest.raises(ValueError, match="AP_ARTIFACT_PUBLIC_ORIGINS"):
        build_database_publication_services(
            cast(async_sessionmaker[AsyncSession], object()),
            {},
            artifact_object_store=cast(ArtifactObjectStore, object()),
        )

    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()),
        {},
        settings=AppSettings(
            artifact_public_origins=("https://artifacts.example.test",)
        ),
        artifact_object_store=cast(ArtifactObjectStore, object()),
    )

    assert services.artifact is not None


def test_skill_composition_requires_trusted_artifact_reader() -> None:
    with pytest.raises(ValueError, match="Trusted Skill Artifact"):
        build_database_publication_services(
            cast(async_sessionmaker[AsyncSession], object()),
            {},
            skill_scanner=cast(SkillSupplyChainScanner, object()),
        )

    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()),
        {},
        trusted_skill_artifact_reader=cast(TrustedSkillArtifactContentReader, object()),
    )

    assert isinstance(services.skill, SkillManagementService)
