"""API health endpoint tests."""

import httpx
import pytest

from apps.api.app import create_app
from packages.infrastructure.public import AppSettings


@pytest.mark.asyncio
async def test_liveness_reports_process_without_external_dependency_checks() -> None:
    application = create_app(AppSettings.model_validate({}))
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service_name": "api"}


@pytest.mark.asyncio
async def test_readiness_reports_validated_configuration() -> None:
    settings = AppSettings.model_validate({"service_name": "api-test"})
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service_name": "api-test"}


@pytest.mark.asyncio
async def test_metrics_is_available_only_from_configured_internal_network() -> None:
    settings = AppSettings.model_validate(
        {"metrics_allowed_networks": ["127.0.0.1/32"]}
    )
    application = create_app(settings)

    internal_transport = httpx.ASGITransport(app=application, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(
        transport=internal_transport, base_url="http://testserver"
    ) as client:
        allowed = await client.get("/metrics")

    external_transport = httpx.ASGITransport(
        app=application, client=("203.0.113.10", 123)
    )
    async with httpx.AsyncClient(
        transport=external_transport, base_url="http://testserver"
    ) as client:
        denied = await client.get("/metrics")

    assert allowed.status_code == 200
    assert "agent_platform_http_requests_total" in allowed.text
    assert denied.status_code == 403
