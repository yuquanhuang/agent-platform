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
