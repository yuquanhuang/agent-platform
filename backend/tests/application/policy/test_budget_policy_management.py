"""BudgetPolicy authorization and immutable version DTO mapping."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.policy import BudgetPolicyManagementService, BudgetPolicyStore
from packages.application.public import RequestMetadata, TenantAccessResolver
from packages.contracts.generated.resources_models import BudgetPolicyCreateRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    BudgetPolicyRecord,
    BudgetPolicyVersionRecord,
    MutationOutcome,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 12, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-budget", trace_id="trace-budget")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="budget-admin",
        display_name="Budget Admin",
        auth_time=NOW,
    )


class BudgetPolicyStub:
    def __init__(self, permissions: frozenset[str]) -> None:
        self.permissions = permissions

    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del authenticated
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
        del context, kwargs
        return MutationOutcome(value=_record())


def _record() -> BudgetPolicyRecord:
    return BudgetPolicyRecord(
        id=POLICY_ID,
        tenant_id=TENANT_ID,
        name="Default model budget",
        description=None,
        status="ACTIVE",
        current_version=BudgetPolicyVersionRecord(
            id=VERSION_ID,
            tenant_id=TENANT_ID,
            policy_id=POLICY_ID,
            version_no=1,
            period="MONTHLY",
            enforcement="HARD",
            token_limit=100_000,
            cost_limit_amount=None,
            cost_limit_currency=None,
            price_catalog_version=None,
            content_hash="sha256:" + "a" * 64,
            created_by=ACTOR_ID,
            created_at=NOW,
        ),
        resource_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def service(stub: BudgetPolicyStub) -> BudgetPolicyManagementService:
    return BudgetPolicyManagementService(
        cast(TenantAccessResolver, stub), cast(BudgetPolicyStore, stub)
    )


@pytest.mark.asyncio
async def test_create_budget_policy_maps_hard_token_version_and_etag() -> None:
    result, etag = await service(
        BudgetPolicyStub(frozenset({"budget_policy:create"}))
    ).create_policy(
        principal(),
        request=BudgetPolicyCreateRequest(
            name="Default model budget",
            description=None,
            period="MONTHLY",
            token_limit=100_000,
        ),
        idempotency_key="budget-create-1",
        metadata=METADATA,
    )

    assert etag == '"rv:1"'
    assert result.current_version.period == "MONTHLY"
    assert result.current_version.enforcement == "HARD"
    assert result.current_version.cost_limit is None


@pytest.mark.asyncio
async def test_create_budget_policy_requires_permission() -> None:
    with pytest.raises(PlatformError) as denied:
        await service(BudgetPolicyStub(frozenset())).create_policy(
            principal(),
            request=BudgetPolicyCreateRequest(
                name="Denied", description=None, period="DAILY", token_limit=10
            ),
            idempotency_key="budget-create-2",
            metadata=METADATA,
        )

    assert denied.value.code == "PERMISSION_DENIED"
