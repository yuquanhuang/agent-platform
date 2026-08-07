"""Agent Draft route registration and ETag tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    AgentManagementService,
    AgentRegistry,
    RequestMetadata,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
)
from packages.domain.public import AgentRecord, AgentReferenceRecord, TenantAccess
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 7, tzinfo=UTC)


class AgentStub:
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
            permissions=frozenset({"agent:read"}),
        )

    async def get_agent(
        self, context: TenantContext, *, agent_id: UUID
    ) -> AgentRecord | None:
        return AgentRecord(
            id=agent_id,
            tenant_id=TENANT_ID,
            code="support_agent",
            name="Support Agent",
            description=None,
            runtime_type="agentscope",
            visibility="tenant",
            tags=(),
            bindings=(),
            status="DRAFT",
            resource_version=3,
            active_deployment_id=None,
            owner_user_id=ACTOR_ID,
            created_at=NOW,
            updated_at=NOW,
        )

    async def list_agent_references(
        self, context: TenantContext, *, agent_id: UUID
    ) -> list[AgentReferenceRecord] | None:
        return [
            AgentReferenceRecord(
                resource_type="agent",
                resource_id=AGENT_ID,
                reference_type="child_agent",
            )
        ]


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = AgentStub()
    service = AgentManagementService(stub, cast(AgentRegistry, stub))
    return create_app(settings, identity_provider=provider, agent_service=service)


@pytest.mark.asyncio
async def test_get_agent_returns_frozen_shape_and_strong_etag() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/agents/{AGENT_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-agent"},
        )

    assert response.status_code == 200
    assert response.headers["ETag"] == '"rv:3"'
    assert response.headers["X-Request-ID"] == "req-agent"
    assert response.json()["runtime_type"] == "agentscope"


@pytest.mark.asyncio
async def test_list_agent_references_returns_safe_reference_shape() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/agents/{AGENT_ID}/references",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-agent-ref"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "resource_type": "agent",
                "resource_id": str(AGENT_ID),
                "reference_type": "child_agent",
            }
        ],
        "next_cursor": None,
        "has_more": False,
    }


def test_app_openapi_registers_all_frozen_agent_draft_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "createAgent",
        "listAgents",
        "getAgent",
        "updateAgent",
        "copyAgent",
        "disableAgent",
        "deleteAgent",
        "listAgentReferences",
    } <= operation_ids
