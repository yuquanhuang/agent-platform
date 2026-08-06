"""Frozen IAM route, authentication and ETag behavior tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import IamManagementService, IamPersistence
from packages.contracts.public import AuthenticatedPrincipal
from packages.domain.public import MutationOutcome, TenantRecord
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC)


class TenantCreatePersistenceStub:
    async def resolve_platform_actor(
        self, principal: AuthenticatedPrincipal
    ) -> UUID | None:
        return ACTOR_ID

    async def create_tenant(self, **kwargs: object) -> MutationOutcome[TenantRecord]:
        return MutationOutcome(
            value=TenantRecord(
                id=TENANT_ID,
                code="tenant-a",
                name="Tenant A",
                status="ACTIVE",
                resource_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    service = IamManagementService(cast(IamPersistence, TenantCreatePersistenceStub()))
    return create_app(settings, identity_provider=provider, iam_service=service)


@pytest.mark.asyncio
async def test_create_tenant_matches_frozen_status_etag_and_request_id() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tenants",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "tenant-create-1",
                "X-Request-ID": "req-tenant-create",
            },
            json={"code": "tenant-a", "name": "Tenant A"},
        )

    assert response.status_code == 201
    assert response.headers["ETag"] == '"rv:1"'
    assert response.headers["X-Request-ID"] == "req-tenant-create"
    assert response.json()["id"] == str(TENANT_ID)


@pytest.mark.asyncio
async def test_iam_route_rejects_missing_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/tenants")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_app_openapi_registers_all_frozen_iam_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "listTenants",
        "createTenant",
        "getTenant",
        "updateTenant",
        "disableTenant",
        "enableTenant",
        "listMembers",
        "createMember",
        "getMember",
        "updateMember",
        "deleteMember",
        "listRoles",
        "createRole",
        "getRole",
        "updateRole",
        "deleteRole",
        "getOperation",
    } <= operation_ids
