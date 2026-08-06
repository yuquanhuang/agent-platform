"""Prompt route contract registration and strong ETag behavior tests."""

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
    PromptManagementService,
    PromptRegistry,
    RequestMetadata,
    TenantAccessResolver,
)
from packages.contracts.generated.resource_content import ResourceContentPrompt
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
)
from packages.domain.public import ResourceDefinitionRecord, TenantAccess
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RESOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 6, tzinfo=UTC)


class PromptStub:
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
            permissions=frozenset({"prompt:read"}),
        )

    async def get_definition(self, context: TenantContext, **kwargs: object):
        return ResourceDefinitionRecord(
            id=RESOURCE_ID,
            tenant_id=TENANT_ID,
            resource_type="prompt",
            code="welcome_prompt",
            name="Welcome Prompt",
            description=None,
            owner_user_id=ACTOR_ID,
            visibility="tenant",
            content_schema_version="1.0",
            content=ResourceContentPrompt(
                resource_type="prompt",
                template="Hello",
                variables=[],
                language="en",
                compiler_policy_version="1",
            ),
            status="DRAFT",
            resource_version=3,
            created_at=NOW,
            updated_at=NOW,
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = PromptStub()
    service = PromptManagementService(
        cast(TenantAccessResolver, stub),
        cast(PromptRegistry, stub),
        CompositeResourceReferenceReader([]),
    )
    return create_app(settings, identity_provider=provider, prompt_service=service)


@pytest.mark.asyncio
async def test_get_prompt_returns_frozen_shape_etag_and_request_id() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/prompts/{RESOURCE_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-prompt"},
        )

    assert response.status_code == 200
    assert response.headers["ETag"] == '"rv:3"'
    assert response.headers["X-Request-ID"] == "req-prompt"
    assert response.json()["resource_type"] == "prompt"


def test_app_openapi_registers_all_frozen_prompt_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "listPrompts",
        "createPrompt",
        "getPrompt",
        "updatePrompt",
        "deletePrompt",
        "publishPrompt",
        "copyPrompt",
        "disablePrompt",
        "enablePrompt",
        "rollbackPrompt",
        "listPromptVersions",
        "diffPromptVersions",
        "listPromptReferences",
    } <= operation_ids
