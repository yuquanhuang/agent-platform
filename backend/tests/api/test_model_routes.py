"""Model resource route contract and asynchronous operation tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    CompositeResourceReferenceReader,
    ModelManagementService,
    ModelRegistry,
    RequestMetadata,
    TenantAccessResolver,
)
from packages.contracts.generated.resource_content import ResourceContentModelProvider
from packages.contracts.public import AuthenticatedPrincipal, SubjectType, TenantContext
from packages.domain.public import (
    MutationOutcome,
    OperationRecord,
    ResourceDefinitionRecord,
    TenantAccess,
)
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RESOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
OPERATION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 7, tzinfo=UTC)


class ModelStub:
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
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
            permissions=frozenset({"model_provider:read", "model_provider:execute"}),
        )

    async def get_definition(
        self, context: TenantContext, **kwargs: object
    ) -> ResourceDefinitionRecord:
        return ResourceDefinitionRecord(
            id=RESOURCE_ID,
            tenant_id=TENANT_ID,
            resource_type="model_provider",
            code="primary_openai",
            name="Primary OpenAI",
            description=None,
            owner_user_id=ACTOR_ID,
            visibility="tenant",
            content_schema_version="1.0",
            content=ResourceContentModelProvider(
                resource_type="model_provider",
                provider_type="openai",
                base_url="https://api.openai.com/v1",
                secret_ref="secret://tenant/tenant-a/model/openai",
                timeout_seconds=30,
                data_retention_policy=None,
            ),
            status="DRAFT",
            resource_version=3,
            created_at=NOW,
            updated_at=NOW,
        )

    async def request_model_provider_connection_test(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[OperationRecord]:
        return MutationOutcome(
            value=OperationRecord(
                id=OPERATION_ID,
                operation_type="model_provider.connection_test",
                status="ACCEPTED",
                resource_type="model_provider",
                resource_id=RESOURCE_ID,
                result=None,
                error=None,
                created_at=NOW,
                updated_at=NOW,
                finished_at=None,
            )
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = ModelStub()
    service = ModelManagementService(
        cast(TenantAccessResolver, stub),
        cast(ModelRegistry, stub),
        CompositeResourceReferenceReader([]),
    )
    return create_app(settings, identity_provider=provider, model_service=service)


@pytest.mark.asyncio
async def test_model_provider_get_and_connection_test_use_frozen_shapes() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        get_response = await client.get(
            f"/api/v1/model-providers/{RESOURCE_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-model"},
        )
        test_response = await client.post(
            f"/api/v1/model-providers/{RESOURCE_ID}/test",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "model-test-key",
            },
        )

    assert get_response.status_code == 200
    assert get_response.headers["ETag"] == '"rv:3"'
    assert get_response.json()["content"]["secret_ref"].startswith("secret://")
    assert test_response.status_code == 202
    assert test_response.json() == {
        "operation_id": str(OPERATION_ID),
        "status": "ACCEPTED",
        "status_url": f"/api/v1/operations/{OPERATION_ID}",
    }


def test_app_openapi_registers_all_frozen_model_resource_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "listModelProviders",
        "createModelProvider",
        "getModelProvider",
        "updateModelProvider",
        "deleteModelProvider",
        "testModelProviderConnection",
        "disableModelProvider",
        "enableModelProvider",
        "listModelConfigs",
        "createModelConfig",
        "getModelConfig",
        "updateModelConfig",
        "deleteModelConfig",
        "publishModelConfig",
        "disableModelConfig",
        "enableModelConfig",
        "rollbackModelConfig",
        "listModelConfigVersions",
        "diffModelConfigVersions",
        "listModelConfigReferences",
    } <= operation_ids
