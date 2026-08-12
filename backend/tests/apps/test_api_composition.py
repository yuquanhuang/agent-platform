"""Deployment-facing API composition tests."""

from typing import cast
from uuid import UUID

import pytest
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition import build_database_publication_services
from packages.application.public import (
    ArtifactDownloadObjectReader,
    ArtifactObjectStore,
    AuditManagementService,
    DeploymentManagementService,
    IamManagementService,
    McpManagementService,
    MessageHistoryService,
    PublicationQueryService,
    QuotaPolicyManagementService,
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
from packages.infrastructure.public import AppSettings, AuthMode, DeploymentEnvironment


def test_database_publication_composition_builds_all_http_services() -> None:
    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()), {}
    )

    assert isinstance(services.release, ReleaseManagementService)
    assert isinstance(services.iam, IamManagementService)
    assert isinstance(services.quota_policy, QuotaPolicyManagementService)
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
            public_base_url=AnyHttpUrl("https://artifacts.example.test"),
            artifact_public_origins=("https://artifacts.example.test",),
        ),
        artifact_object_store=cast(ArtifactObjectStore, object()),
        artifact_download_reader=cast(ArtifactDownloadObjectReader, object()),
        artifact_download_gateway_subject_id=UUID(
            "11111111-1111-4111-8111-111111111111"
        ),
    )

    assert services.artifact is not None
    assert services.artifact_gateway is not None


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


def test_production_api_composition_requires_explicit_run_capacity_limits() -> None:
    production = AppSettings(
        env=DeploymentEnvironment.PRODUCTION,
        auth_mode=AuthMode.OIDC,
        oidc_issuer=AnyHttpUrl("https://issuer.example.test"),
        oidc_client_id="agent-platform",
        oidc_client_secret_ref="secret://oidc/client",
    )

    with pytest.raises(ValueError, match="AP_RUN_MAX_NONTERMINAL_PER_TENANT"):
        build_database_publication_services(
            cast(async_sessionmaker[AsyncSession], object()),
            {},
            settings=production,
        )

    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()),
        {},
        settings=production.model_copy(
            update={
                "run_max_nonterminal_per_tenant": 100,
                "run_max_nonterminal_per_user": 10,
                "run_max_nonterminal_per_agent": 50,
                "run_max_nonterminal_agentscope": 100,
                "run_max_nonterminal_codex": 20,
            }
        ),
    )

    assert isinstance(services.run, RunManagementService)
