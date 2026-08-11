"""Sandbox Manager deployment composition tests."""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.sandbox_manager.composition import (
    build_database_sandbox_lifecycle_service,
    create_database_sandbox_manager_app,
)
from apps.sandbox_manager.routes import SandboxServiceIdentityProvider
from packages.application.sandbox import (
    SandboxLifecycleService,
    SandboxPolicyResolver,
    SandboxProvider,
    SandboxProvisionTokenVerifier,
)
from packages.infrastructure.public import AppSettings


class DependencyStub:
    pass


def test_composition_requires_explicit_trusted_adapters_and_registers_routes() -> None:
    settings = AppSettings.model_validate(
        {"env": "test", "sandbox_provider_timeout_seconds": 12}
    )
    session_factory = cast(async_sessionmaker[AsyncSession], object())
    provider = cast(SandboxProvider, DependencyStub())
    resolver = cast(SandboxPolicyResolver, DependencyStub())
    verifier = cast(SandboxProvisionTokenVerifier, DependencyStub())

    service = build_database_sandbox_lifecycle_service(
        settings,
        session_factory=session_factory,
        provider=provider,
        policy_resolver=resolver,
        token_verifier=verifier,
    )
    application = create_database_sandbox_manager_app(
        settings,
        session_factory=session_factory,
        identity_provider=cast(SandboxServiceIdentityProvider, DependencyStub()),
        provider=provider,
        policy_resolver=resolver,
        token_verifier=verifier,
    )

    assert isinstance(service, SandboxLifecycleService)
    assert application.title == "Agent Platform Sandbox Manager"
    assert (
        sum(len(operations) for operations in application.openapi()["paths"].values())
        == 9
    )
