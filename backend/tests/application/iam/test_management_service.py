"""IAM application authorization and contract mapping tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    IamManagementService,
    IamPersistence,
    RequestMetadata,
)
from packages.contracts.generated.resources_models import (
    RoleCreateRequest,
    TenantCreateRequest,
    TenantUpdateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MutationOutcome,
    OperationRecord,
    TenantAccess,
    TenantRecord,
)

ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-1", trace_id="trace-1")
OPERATION_ID = UUID("33333333-3333-4333-8333-333333333333")


def principal(*, platform_admin: bool = True) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="subject-1",
        display_name="Admin",
        platform_roles=frozenset({"platform_admin"}) if platform_admin else frozenset(),
        auth_time=NOW,
    )


class PlatformPersistenceStub:
    async def resolve_platform_actor(
        self, authenticated: AuthenticatedPrincipal
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


class SessionOperationPersistenceStub:
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
            permissions=frozenset({"session:read"}),
        )

    async def get_operation(
        self, access: TenantAccess, operation_id: UUID
    ) -> OperationRecord | None:
        return OperationRecord(
            id=operation_id,
            operation_type="session.delete",
            status="SUCCEEDED",
            resource_type="session",
            resource_id=OPERATION_ID,
            result={"status": "DELETED"},
            error=None,
            created_at=NOW,
            updated_at=NOW,
            finished_at=NOW,
        )


class ArtifactOperationPersistenceStub(SessionOperationPersistenceStub):
    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        access = await super().resolve_tenant_access(authenticated, metadata)
        return TenantAccess(
            context=access.context,
            permissions=frozenset({"artifact:read"}),
        )

    async def get_operation(
        self, access: TenantAccess, operation_id: UUID
    ) -> OperationRecord | None:
        return OperationRecord(
            id=operation_id,
            operation_type="artifact.delete",
            status="SUCCEEDED",
            resource_type="artifact",
            resource_id=OPERATION_ID,
            result={"status": "DELETED"},
            error=None,
            created_at=NOW,
            updated_at=NOW,
            finished_at=NOW,
        )


@pytest.mark.asyncio
async def test_create_tenant_requires_platform_admin_and_returns_strong_etag() -> None:
    service = IamManagementService(cast(IamPersistence, PlatformPersistenceStub()))

    tenant, etag = await service.create_tenant(
        principal(),
        request=TenantCreateRequest(code="tenant-a", name="Tenant A"),
        idempotency_key="tenant-create-1",
        metadata=METADATA,
    )

    assert tenant.id == str(TENANT_ID)
    assert etag == '"rv:1"'

    with pytest.raises(PlatformError) as denied:
        await service.create_tenant(
            principal(platform_admin=False),
            request=TenantCreateRequest(code="tenant-a", name="Tenant A"),
            idempotency_key="tenant-create-2",
            metadata=METADATA,
        )
    assert denied.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_empty_update_and_invalid_permission_fail_before_persistence() -> None:
    service = IamManagementService(cast(IamPersistence, PlatformPersistenceStub()))

    with pytest.raises(PlatformError) as empty_update:
        await service.update_tenant(
            principal(),
            tenant_id=str(TENANT_ID),
            if_match='"rv:1"',
            request=TenantUpdateRequest.model_validate({}),
            metadata=METADATA,
        )
    assert empty_update.value.code == "VALIDATION_ERROR"

    with pytest.raises(PlatformError) as invalid_permission:
        await service.create_role(
            principal(),
            request=RoleCreateRequest(
                code="bad_role",
                name="Bad Role",
                description=None,
                permissions=["database:owner"],
            ),
            idempotency_key="role-create-1",
            metadata=METADATA,
        )
    assert invalid_permission.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_session_read_permission_can_poll_delete_operation() -> None:
    service = IamManagementService(
        cast(IamPersistence, SessionOperationPersistenceStub())
    )

    operation = await service.get_operation(
        principal(platform_admin=False), str(OPERATION_ID), METADATA
    )

    assert operation.operation_type == "session.delete"
    assert operation.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_artifact_read_permission_can_poll_delete_operation() -> None:
    service = IamManagementService(
        cast(IamPersistence, ArtifactOperationPersistenceStub())
    )

    operation = await service.get_operation(
        principal(platform_admin=False), str(OPERATION_ID), METADATA
    )

    assert operation.operation_type == "artifact.delete"
    assert operation.status == "SUCCEEDED"
