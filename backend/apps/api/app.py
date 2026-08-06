"""FastAPI application factory."""

from fastapi import FastAPI

from apps.api.http import RequestContextMiddleware, install_exception_handlers
from apps.api.routes.auth import create_auth_router
from apps.api.routes.health import create_health_router
from apps.api.routes.iam import create_iam_router
from apps.api.routes.metrics import create_metrics_router
from apps.api.routes.prompts import create_prompt_router
from packages.application.public import (
    CurrentIdentityService,
    HealthService,
    IamManagementService,
    IdentityReader,
    PromptManagementService,
)
from packages.contracts.public import IdentityProvider
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import AppSettings, AuthMode, get_settings


def create_app(
    settings: AppSettings | None = None,
    *,
    identity_reader: IdentityReader | None = None,
    identity_provider: IdentityProvider | None = None,
    iam_service: IamManagementService | None = None,
    prompt_service: PromptManagementService | None = None,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build the API application without connecting to external dependencies."""

    resolved_settings = settings or get_settings()
    resolved_metrics = metrics or PlatformMetrics()
    application = FastAPI(
        title="Agent Platform API",
        version="0.1.0",
    )
    application.state.metrics = resolved_metrics
    application.add_middleware(RequestContextMiddleware, metrics=resolved_metrics)
    install_exception_handlers(application)
    health_service = HealthService(service_name=resolved_settings.service_name)
    resolved_identity_provider = identity_provider
    if (
        resolved_identity_provider is None
        and resolved_settings.auth_mode is AuthMode.MOCK
    ):
        resolved_identity_provider = MockIdentityProvider(resolved_settings)
    identity_service = (
        CurrentIdentityService(identity_reader) if identity_reader is not None else None
    )
    application.include_router(create_health_router(health_service))
    application.include_router(
        create_auth_router(resolved_identity_provider, identity_service)
    )
    application.include_router(
        create_iam_router(resolved_identity_provider, iam_service)
    )
    application.include_router(
        create_prompt_router(resolved_identity_provider, prompt_service)
    )
    application.include_router(
        create_metrics_router(
            resolved_metrics,
            resolved_settings.metrics_allowed_networks,
        )
    )
    return application


app = create_app()
