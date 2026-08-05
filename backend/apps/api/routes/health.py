"""Process health endpoints."""

from fastapi import APIRouter

from packages.application.public import HealthService
from packages.contracts.public import HealthResponse


def create_health_router(health_service: HealthService) -> APIRouter:
    """Create health routes bound to the current process health service."""

    router = APIRouter(tags=["health"])
    router.add_api_route(
        "/health/live",
        health_service.live,
        methods=["GET"],
        response_model=HealthResponse,
    )
    router.add_api_route(
        "/health/ready",
        health_service.ready,
        methods=["GET"],
        response_model=HealthResponse,
    )
    return router
