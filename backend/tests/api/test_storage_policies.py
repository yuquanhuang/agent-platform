"""StoragePolicy route contract and strong ETag tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.policy import (
    StoragePolicyManagementService,
    StoragePolicyStore,
    TenantStoragePolicy,
)
from packages.application.public import RequestMetadata, TenantAccessResolver
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MutationOutcome,
    StorageLimitsRecord,
    StoragePolicyRecord,
    StoragePolicyVersionRecord,
    TenantAccess,
)
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 12, tzinfo=UTC)


class StoragePolicyRouteStub:
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR_ID),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset({"storage_policy:create"}),
        )

    async def create_policy(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return MutationOutcome(
            value=StoragePolicyRecord(
                id=POLICY_ID,
                tenant_id=TENANT_ID,
                name="Default storage limits",
                description=None,
                status="ACTIVE",
                current_version=StoragePolicyVersionRecord(
                    id=VERSION_ID,
                    tenant_id=TENANT_ID,
                    policy_id=POLICY_ID,
                    version_no=1,
                    limits=StorageLimitsRecord(
                        max_reserved_workspace_bytes=2_147_483_648,
                        max_reserved_workspaces=20,
                        max_reserved_artifact_bytes=1_073_741_824,
                        max_reserved_artifacts=200,
                    ),
                    content_hash="sha256:" + "a" * 64,
                    created_by=ACTOR_ID,
                    created_at=NOW,
                ),
                resource_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = StoragePolicyRouteStub()
    service = StoragePolicyManagementService(
        cast(TenantAccessResolver, stub),
        cast(StoragePolicyStore, stub),
        TenantStoragePolicy(
            max_reserved_workspace_bytes=10_737_418_240,
            max_reserved_workspaces=100,
            max_reserved_artifact_bytes=5_368_709_120,
            max_reserved_artifacts=1_000,
        ),
    )
    return create_app(
        settings, identity_provider=provider, storage_policy_service=service
    )


@pytest.mark.asyncio
async def test_create_storage_policy_matches_frozen_shape_and_etag() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/storage-policies",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "storage-create-1",
                "X-Request-ID": "req-storage",
            },
            json={
                "name": "Default storage limits",
                "limits": {
                    "max_reserved_workspace_bytes": 2_147_483_648,
                    "max_reserved_workspaces": 20,
                    "max_reserved_artifact_bytes": 1_073_741_824,
                    "max_reserved_artifacts": 200,
                },
            },
        )

    assert response.status_code == 201
    assert response.headers["ETag"] == '"rv:1"'
    assert response.json()["current_version"]["limits"] == {
        "max_reserved_workspace_bytes": 2_147_483_648,
        "max_reserved_workspaces": 20,
        "max_reserved_artifact_bytes": 1_073_741_824,
        "max_reserved_artifacts": 200,
    }


def test_app_openapi_registers_all_storage_policy_operations() -> None:
    operation_ids = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )
    assert {
        "listStoragePolicies",
        "createStoragePolicy",
        "getStoragePolicy",
        "updateStoragePolicy",
        "disableStoragePolicy",
        "enableStoragePolicy",
        "listStoragePolicyVersions",
    } <= operation_ids
