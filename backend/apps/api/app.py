"""FastAPI application factory."""

from fastapi import FastAPI

from apps.api.routes.health import create_health_router
from packages.application.public import HealthService
from packages.infrastructure.public import AppSettings, get_settings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build the API application without connecting to external dependencies."""

    resolved_settings = settings or get_settings()
    application = FastAPI(
        title="Agent Platform API",
        version="0.1.0",
    )
    health_service = HealthService(service_name=resolved_settings.service_name)
    application.include_router(create_health_router(health_service))
    return application


app = create_app()
