"""Deployment-facing composition for the independent Sandbox Manager."""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.sandbox_manager.app import create_sandbox_manager_app
from apps.sandbox_manager.routes import SandboxServiceIdentityProvider
from packages.application.sandbox import (
    SandboxLifecycleService,
    SandboxPolicyResolver,
    SandboxProvider,
    SandboxProvisionTokenVerifier,
)
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import AppSettings, SqlAlchemySandboxLifecycleStore


def build_database_sandbox_lifecycle_service(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    provider: SandboxProvider,
    policy_resolver: SandboxPolicyResolver,
    token_verifier: SandboxProvisionTokenVerifier,
) -> SandboxLifecycleService:
    """Build the lifecycle service while keeping deployment adapters explicit."""

    return SandboxLifecycleService(
        SqlAlchemySandboxLifecycleStore(session_factory),
        provider,
        policy_resolver,
        token_verifier,
        provider_timeout_seconds=settings.sandbox_provider_timeout_seconds,
    )


def create_database_sandbox_manager_app(
    settings: AppSettings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    identity_provider: SandboxServiceIdentityProvider,
    provider: SandboxProvider,
    policy_resolver: SandboxPolicyResolver,
    token_verifier: SandboxProvisionTokenVerifier,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Compose trusted adapters without selecting or weakening Provider isolation."""

    service = build_database_sandbox_lifecycle_service(
        settings,
        session_factory=session_factory,
        provider=provider,
        policy_resolver=policy_resolver,
        token_verifier=token_verifier,
    )
    return create_sandbox_manager_app(
        identity_provider=identity_provider,
        service=service,
        metrics=metrics,
    )
