"""Deployment-facing API composition tests."""

# pyright: reportPrivateUsage=false

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
    BudgetPolicyManagementService,
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
    StoragePolicyManagementService,
    TrustedSkillArtifactContentReader,
)
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import AppSettings, AuthMode, DeploymentEnvironment


def test_database_publication_composition_builds_all_http_services() -> None:
    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()), {}
    )

    assert isinstance(services.release, ReleaseManagementService)
    assert isinstance(services.iam, IamManagementService)
    assert isinstance(services.quota_policy, QuotaPolicyManagementService)
    assert isinstance(services.budget_policy, BudgetPolicyManagementService)
    assert isinstance(services.storage_policy, StoragePolicyManagementService)
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
    assert isinstance(services.metrics, PlatformMetrics)
    assert services.event._metrics is services.metrics
    assert services.event_stream._metrics is services.metrics


def test_database_publication_composition_reuses_explicit_metrics() -> None:
    metrics = PlatformMetrics()

    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()), {}, metrics=metrics
    )

    assert services.metrics is metrics
    assert services.event._metrics is metrics
    assert services.event_stream._metrics is metrics


def test_storage_policy_composition_includes_workspace_deployment_limits() -> None:
    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()),
        {},
        settings=AppSettings(
            workspace_max_reserved_bytes_per_tenant=2_147_483_648,
            workspace_max_reserved_count_per_tenant=20,
        ),
    )

    policy = services.storage_policy._deployment_policy
    assert policy.max_reserved_workspace_bytes == 2_147_483_648
    assert policy.max_reserved_workspaces == 20


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


def test_production_artifact_composition_requires_storage_capacity_limits() -> None:
    production = AppSettings(
        env=DeploymentEnvironment.PRODUCTION,
        auth_mode=AuthMode.OIDC,
        oidc_issuer=AnyHttpUrl("https://issuer.example.test"),
        oidc_client_id="agent-platform",
        oidc_client_secret_ref="secret://oidc/client",
        artifact_public_origins=("https://artifacts.example.test",),
        run_max_nonterminal_per_tenant=100,
        run_max_nonterminal_per_user=10,
        run_max_nonterminal_per_agent=50,
        run_max_nonterminal_agentscope=100,
        run_max_nonterminal_codex=20,
    )

    with pytest.raises(ValueError, match="AP_ARTIFACT_MAX_RESERVED_BYTES_PER_TENANT"):
        build_database_publication_services(
            cast(async_sessionmaker[AsyncSession], object()),
            {},
            settings=production,
            artifact_object_store=cast(ArtifactObjectStore, object()),
            artifact_download_reader=cast(ArtifactDownloadObjectReader, object()),
            artifact_download_gateway_subject_id=UUID(
                "11111111-1111-4111-8111-111111111111"
            ),
        )

    services = build_database_publication_services(
        cast(async_sessionmaker[AsyncSession], object()),
        {},
        settings=production.model_copy(
            update={
                "artifact_max_reserved_bytes_per_tenant": 1_073_741_824,
                "artifact_max_reserved_count_per_tenant": 10_000,
            }
        ),
        artifact_object_store=cast(ArtifactObjectStore, object()),
        artifact_download_reader=cast(ArtifactDownloadObjectReader, object()),
        artifact_download_gateway_subject_id=UUID(
            "11111111-1111-4111-8111-111111111111"
        ),
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
