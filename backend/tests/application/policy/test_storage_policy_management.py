"""StoragePolicy authorization, hard-limit validation, and DTO mapping."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.policy import (
    StoragePolicyManagementService,
    StoragePolicyStore,
    TenantStoragePolicy,
)
from packages.application.public import RequestMetadata, TenantAccessResolver
from packages.contracts.generated.resources_models import (
    StorageLimits,
    StoragePolicyCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
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

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 12, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-storage", trace_id="trace-storage")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="storage-admin",
        display_name="Storage Admin",
        auth_time=NOW,
    )


class StoragePolicyStub:
    def __init__(self, permissions: frozenset[str]) -> None:
        self.permissions = permissions
        self.received_limits: TenantStoragePolicy | None = None

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

    async def create_policy(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[StoragePolicyRecord]:
        del context
        self.received_limits = cast(TenantStoragePolicy, kwargs["limits"])
        return MutationOutcome(value=_record(self.received_limits))


def _record(limits: TenantStoragePolicy) -> StoragePolicyRecord:
    return StoragePolicyRecord(
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
                max_reserved_workspace_bytes=limits.max_reserved_workspace_bytes,
                max_reserved_workspaces=limits.max_reserved_workspaces,
                max_reserved_artifact_bytes=limits.max_reserved_artifact_bytes,
                max_reserved_artifacts=limits.max_reserved_artifacts,
            ),
            content_hash="sha256:" + "a" * 64,
            created_by=ACTOR_ID,
            created_at=NOW,
        ),
        resource_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def service(stub: StoragePolicyStub) -> StoragePolicyManagementService:
    return StoragePolicyManagementService(
        cast(TenantAccessResolver, stub),
        cast(StoragePolicyStore, stub),
        TenantStoragePolicy(
            max_reserved_workspace_bytes=10_737_418_240,
            max_reserved_workspaces=100,
            max_reserved_artifact_bytes=5_368_709_120,
            max_reserved_artifacts=1_000,
        ),
    )


@pytest.mark.asyncio
async def test_create_storage_policy_maps_separate_pools_and_etag() -> None:
    stub = StoragePolicyStub(frozenset({"storage_policy:create"}))

    result, etag = await service(stub).create_policy(
        principal(),
        request=StoragePolicyCreateRequest(
            name="Default storage limits",
            description=None,
            limits=StorageLimits(
                max_reserved_workspace_bytes=2_147_483_648,
                max_reserved_workspaces=20,
                max_reserved_artifact_bytes=1_073_741_824,
                max_reserved_artifacts=200,
            ),
        ),
        idempotency_key="storage-create-1",
        metadata=METADATA,
    )

    assert etag == '"rv:1"'
    assert result.current_version.limits.max_reserved_workspaces == 20
    assert result.current_version.limits.max_reserved_artifacts == 200
    assert stub.received_limits == TenantStoragePolicy(
        max_reserved_workspace_bytes=2_147_483_648,
        max_reserved_workspaces=20,
        max_reserved_artifact_bytes=1_073_741_824,
        max_reserved_artifacts=200,
    )


@pytest.mark.asyncio
async def test_create_storage_policy_rejects_deployment_limit_expansion() -> None:
    stub = StoragePolicyStub(frozenset({"storage_policy:create"}))

    with pytest.raises(PlatformError) as denied:
        await service(stub).create_policy(
            principal(),
            request=StoragePolicyCreateRequest(
                name="Too broad",
                description=None,
                limits=StorageLimits(
                    max_reserved_workspace_bytes=None,
                    max_reserved_workspaces=101,
                    max_reserved_artifact_bytes=None,
                    max_reserved_artifacts=None,
                ),
            ),
            idempotency_key="storage-create-2",
            metadata=METADATA,
        )

    assert denied.value.code == "VALIDATION_ERROR"
    assert "max_reserved_workspaces" in denied.value.message


@pytest.mark.asyncio
async def test_create_storage_policy_requires_permission() -> None:
    with pytest.raises(PlatformError) as denied:
        await service(StoragePolicyStub(frozenset())).create_policy(
            principal(),
            request=StoragePolicyCreateRequest(
                name="Denied",
                description=None,
                limits=StorageLimits(
                    max_reserved_workspace_bytes=None,
                    max_reserved_workspaces=None,
                    max_reserved_artifact_bytes=None,
                    max_reserved_artifacts=10,
                ),
            ),
            idempotency_key="storage-create-3",
            metadata=METADATA,
        )

    assert denied.value.code == "PERMISSION_DENIED"
