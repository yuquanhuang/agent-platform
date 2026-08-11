"""Sandbox Manager FastAPI factory kept independent from the public API process."""

from fastapi import FastAPI

from apps.api.http import RequestContextMiddleware, install_exception_handlers
from apps.sandbox_manager.routes import (
    SandboxServiceIdentityProvider,
    create_sandbox_router,
)
from packages.application.sandbox import SandboxInternalService
from packages.infrastructure.observability import PlatformMetrics


def create_sandbox_manager_app(
    *,
    identity_provider: SandboxServiceIdentityProvider | None = None,
    service: SandboxInternalService | None = None,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build the internal app without connecting to a Provider or data store."""

    resolved_metrics = metrics or PlatformMetrics()
    application = FastAPI(title="Agent Platform Sandbox Manager", version="0.1.0")
    application.state.metrics = resolved_metrics
    application.add_middleware(RequestContextMiddleware, metrics=resolved_metrics)
    install_exception_handlers(application)
    application.include_router(create_sandbox_router(identity_provider, service))
    return application
