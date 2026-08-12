"""QuotaPolicy management validation, authorization and immutable version mapping."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.policy import (
    QuotaPolicyManagementService,
    QuotaPolicyStore,
    RunCapacityPolicy,
)
from packages.application.public import RequestMetadata, TenantAccessResolver
from packages.contracts.generated.resources_models import (
    QuotaPolicyCreateRequest,
    RunCapacityLimits,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MutationOutcome,
    QuotaPolicyRecord,
    QuotaPolicyVersionRecord,
    RunCapacityLimitsRecord,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 12, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-quota", trace_id="trace-quota")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="quota-admin",
        display_name="Quota Admin",
        auth_time=NOW,
    )


class QuotaPolicyStub:
    def __init__(self, permissions: frozenset[str]) -> None:
        self.permissions = permissions
        self.received_policy: RunCapacityPolicy | None = None

    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
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
            permissions=self.permissions,
        )

    async def create_policy(self, context: TenantContext, **kwargs: object):
        self.received_policy = cast(RunCapacityPolicy, kwargs["limits"])
        return MutationOutcome(
            value=QuotaPolicyRecord(
                id=POLICY_ID,
                tenant_id=TENANT_ID,
                name="Default Run limits",
                description=None,
                status="ACTIVE",
                current_version=QuotaPolicyVersionRecord(
                    id=VERSION_ID,
                    tenant_id=TENANT_ID,
                    policy_id=POLICY_ID,
                    version_no=1,
                    limits=RunCapacityLimitsRecord(max_nonterminal_runs_per_tenant=10),
                    content_hash="sha256:" + "a" * 64,
                    created_by=ACTOR_ID,
                    created_at=NOW,
                ),
                resource_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def service(stub: QuotaPolicyStub) -> QuotaPolicyManagementService:
    return QuotaPolicyManagementService(
        cast(TenantAccessResolver, stub),
        cast(QuotaPolicyStore, stub),
        RunCapacityPolicy(max_nonterminal_runs_per_tenant=20),
    )


@pytest.mark.asyncio
async def test_create_quota_policy_maps_version_and_strong_etag() -> None:
    stub = QuotaPolicyStub(frozenset({"quota_policy:create"}))

    result, etag = await service(stub).create_policy(
        principal(),
        request=QuotaPolicyCreateRequest(
            name="Default Run limits",
            description=None,
            limits=RunCapacityLimits(
                max_nonterminal_runs_per_tenant=10,
                max_nonterminal_runs_per_user=None,
                max_nonterminal_runs_per_agent=None,
                max_nonterminal_agentscope_runs=None,
                max_nonterminal_codex_runs=None,
            ),
        ),
        idempotency_key="quota-create-1",
        metadata=METADATA,
    )

    assert etag == '"rv:1"'
    assert result.current_version.version_no == 1
    assert result.current_version.limits.max_nonterminal_runs_per_tenant == 10
    assert stub.received_policy is not None


@pytest.mark.asyncio
async def test_create_quota_policy_rejects_expansion_and_missing_permission() -> None:
    with pytest.raises(PlatformError) as expanded:
        await service(
            QuotaPolicyStub(frozenset({"quota_policy:create"}))
        ).create_policy(
            principal(),
            request=QuotaPolicyCreateRequest(
                name="Expanded",
                description=None,
                limits=RunCapacityLimits(
                    max_nonterminal_runs_per_tenant=21,
                    max_nonterminal_runs_per_user=None,
                    max_nonterminal_runs_per_agent=None,
                    max_nonterminal_agentscope_runs=None,
                    max_nonterminal_codex_runs=None,
                ),
            ),
            idempotency_key="quota-create-2",
            metadata=METADATA,
        )
    assert expanded.value.code == "VALIDATION_ERROR"

    with pytest.raises(PlatformError) as denied:
        await service(QuotaPolicyStub(frozenset())).create_policy(
            principal(),
            request=QuotaPolicyCreateRequest(
                name="Denied",
                description=None,
                limits=RunCapacityLimits(
                    max_nonterminal_runs_per_tenant=10,
                    max_nonterminal_runs_per_user=None,
                    max_nonterminal_runs_per_agent=None,
                    max_nonterminal_agentscope_runs=None,
                    max_nonterminal_codex_runs=None,
                ),
            ),
            idempotency_key="quota-create-3",
            metadata=METADATA,
        )
    assert denied.value.code == "PERMISSION_DENIED"
