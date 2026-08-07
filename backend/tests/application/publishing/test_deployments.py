"""Deployment query authorization and frozen DTO mapping tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import RequestMetadata
from packages.application.publishing import (
    DeploymentManagementService,
    DeploymentStore,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import DeploymentRecord, TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
DEPLOYMENT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 7, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-deployment", trace_id="trace-deployment")
PRINCIPAL = AuthenticatedPrincipal(
    identity_issuer="mock",
    external_subject=str(ACTOR_ID),
    display_name="Operator",
    email=None,
    active_tenant_id=str(TENANT_ID),
    membership_version=1,
    auth_time=NOW,
)


class DeploymentStub:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed

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
            permissions=frozenset({"agent:read"} if self.allowed else set()),
        )

    async def get_deployment(
        self, context: TenantContext, *, deployment_id: UUID
    ) -> DeploymentRecord | None:
        if deployment_id != DEPLOYMENT_ID:
            return None
        return DeploymentRecord(
            id=DEPLOYMENT_ID,
            tenant_id=TENANT_ID,
            release_id=UUID("44444444-4444-4444-8444-444444444444"),
            agent_id=UUID("55555555-5555-4555-8555-555555555555"),
            snapshot_id=UUID("66666666-6666-4666-8666-666666666666"),
            bundle_id=UUID("77777777-7777-4777-8777-777777777777"),
            runtime_target_id="rt_agentscope_default",
            status="ACTIVE",
            compatibility_hash="sha256:" + "a" * 64,
            activation_fencing_token=3,
            created_at=NOW,
            activated_at=NOW,
            retired_at=None,
        )


@pytest.mark.asyncio
async def test_get_deployment_maps_internal_history_to_frozen_contract() -> None:
    stub = DeploymentStub()
    service = DeploymentManagementService(stub, cast(DeploymentStore, stub))

    response = await service.get_deployment(
        PRINCIPAL,
        deployment_id=str(DEPLOYMENT_ID),
        metadata=METADATA,
    )

    assert response.id == str(DEPLOYMENT_ID)
    assert response.status == "ACTIVE"
    assert response.runtime_target_id == "rt_agentscope_default"
    assert response.activated_at == NOW


@pytest.mark.asyncio
async def test_get_deployment_requires_agent_read() -> None:
    stub = DeploymentStub(allowed=False)
    service = DeploymentManagementService(stub, cast(DeploymentStore, stub))

    with pytest.raises(PlatformError) as denied:
        await service.get_deployment(
            PRINCIPAL,
            deployment_id=str(DEPLOYMENT_ID),
            metadata=METADATA,
        )

    assert denied.value.code == "PERMISSION_DENIED"
