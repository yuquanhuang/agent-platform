"""Frozen `/api/v1/me` protocol and Mock auth tests."""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from apps.api.app import create_app
from packages.contracts.public import AuthenticatedPrincipal
from packages.domain.public import IdentitySnapshot, MembershipSnapshot
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
ROLE_ID = "33333333-3333-4333-8333-333333333333"
AUTH_TIME = datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC)


class StubIdentityReader:
    async def read(self, principal: AuthenticatedPrincipal) -> IdentitySnapshot:
        return IdentitySnapshot(
            user_id=USER_ID,
            external_subject=principal.external_subject,
            display_name=principal.display_name,
            auth_time=principal.auth_time,
            memberships=(
                MembershipSnapshot(
                    tenant_id=TENANT_ID,
                    tenant_name="Tenant A",
                    status="ACTIVE",
                    role_ids=(ROLE_ID,),
                    membership_version=1,
                ),
            ),
        )


def build_test_app() -> tuple[FastAPI, AppSettings]:
    settings = AppSettings.model_validate(
        {
            "mock_active_tenant_id": TENANT_ID,
            "mock_membership_version": 1,
        }
    )
    provider = MockIdentityProvider(settings, now=lambda: AUTH_TIME)
    application = create_app(
        settings,
        identity_reader=StubIdentityReader(),
        identity_provider=provider,
    )
    return application, settings


@pytest.mark.asyncio
async def test_current_identity_requires_bearer_and_returns_error_envelope() -> None:
    application, _ = build_test_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_current_identity_matches_generated_contract_and_ignores_tenant_header() -> (
    None
):
    application, _ = build_test_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/me",
            headers={
                "Authorization": "Bearer mock",
                "X-Tenant-ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "X-Role": "platform_admin",
                "X-Request-ID": "req-client-1",
            },
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-client-1"
    assert response.json() == {
        "user_id": USER_ID,
        "external_subject": "mock-platform-admin",
        "display_name": "Mock Platform Admin",
        "active_tenant_id": TENANT_ID,
        "memberships": [
            {
                "tenant_id": TENANT_ID,
                "tenant_name": "Tenant A",
                "status": "ACTIVE",
                "role_ids": [ROLE_ID],
                "membership_version": 1,
            }
        ],
        "auth_time": "2026-08-06T01:02:03Z",
    }
