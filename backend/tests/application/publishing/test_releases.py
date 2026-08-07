"""Release request authorization, idempotency response, and safe status tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    ReleaseManagementService,
    ReleaseStore,
    RequestMetadata,
)
from packages.contracts.generated.core_models import (
    PublishAgentRequest,
    RollbackAgentRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MutationOutcome,
    ReleaseRecord,
    ReleaseStatus,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
RELEASE_ID = UUID("44444444-4444-4444-8444-444444444444")
OPERATION_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 7, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-release", trace_id="trace-release")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="publisher",
        display_name="Publisher",
        auth_time=NOW,
    )


def access(*permissions: str) -> TenantAccess:
    return TenantAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.USER,
            subject_id=str(ACTOR_ID),
            membership_version=1,
            auth_time=NOW,
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset(permissions),
    )


def release(*, status: str = "REQUESTED", error_code: str | None = None):
    return ReleaseRecord(
        id=RELEASE_ID,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        requested_by=ACTOR_ID,
        operation_id=OPERATION_ID,
        release_kind="PUBLISH",
        expected_agent_version=3,
        requested_snapshot_id=None,
        runtime_targets=("rt_agentscope_default",),
        release_note="Release",
        run_smoke_test=True,
        activate_on_success=True,
        status=cast(ReleaseStatus, status),
        workflow_id=f"publish/{TENANT_ID}/{RELEASE_ID}",
        snapshot_id=None,
        deployment_ids=(),
        error_code=error_code,
        error_detail=(
            {"message": "Bundle scan failed.", "internal": "not-secret"}
            if error_code
            else None
        ),
        created_at=NOW,
        started_at=None,
        finished_at=None,
    )


class Stub:
    def __init__(self, permissions: tuple[str, ...]) -> None:
        self.permissions = permissions
        self.requested: dict[str, object] = {}
        self.record = release()

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return access(*self.permissions)

    async def request_release(self, context: TenantContext, **kwargs: object):
        self.requested = kwargs
        return MutationOutcome(value=self.record)

    async def request_rollback(self, context: TenantContext, **kwargs: object):
        self.requested = kwargs
        self.record = release()
        return MutationOutcome(value=self.record)

    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None:
        return self.record if release_id == RELEASE_ID else None


@pytest.mark.asyncio
async def test_publish_agent_returns_frozen_release_acceptance() -> None:
    stub = Stub(("agent:publish", "agent:read"))
    service = ReleaseManagementService(stub, cast(ReleaseStore, stub))

    result = await service.publish_agent(
        principal(),
        agent_id=str(AGENT_ID),
        request=PublishAgentRequest(
            expected_agent_version=3,
            runtime_targets=["rt_agentscope_default"],
            release_note="Release",
        ),
        idempotency_key="release-idempotency",
        metadata=METADATA,
    )

    assert result.release_id == str(RELEASE_ID)
    assert result.workflow_id == f"publish/{TENANT_ID}/{RELEASE_ID}"
    assert result.status_url == f"/api/v1/releases/{RELEASE_ID}"
    assert len(cast(str, stub.requested["request_hash"])) == 64


@pytest.mark.asyncio
async def test_publish_agent_rejects_duplicate_runtime_targets() -> None:
    stub = Stub(("agent:publish",))
    service = ReleaseManagementService(stub, cast(ReleaseStore, stub))

    with pytest.raises(PlatformError) as invalid:
        await service.publish_agent(
            principal(),
            agent_id=str(AGENT_ID),
            request=PublishAgentRequest(
                expected_agent_version=3,
                runtime_targets=["rt", "rt"],
                release_note="Release",
            ),
            idempotency_key="release-idempotency",
            metadata=METADATA,
        )

    assert invalid.value.code == "VALIDATION_ERROR"
    assert stub.requested == {}


@pytest.mark.asyncio
async def test_rollback_agent_targets_historical_snapshot_without_draft_version() -> (
    None
):
    stub = Stub(("agent:publish",))
    service = ReleaseManagementService(stub, cast(ReleaseStore, stub))

    result = await service.rollback_agent(
        principal(),
        agent_id=str(AGENT_ID),
        request=RollbackAgentRequest(
            snapshot_id=str(UUID("66666666-6666-4666-8666-666666666666")),
            runtime_targets=["rt_agentscope_default"],
            release_note="Rollback to v1",
        ),
        idempotency_key="rollback-idempotency",
        metadata=METADATA,
    )

    assert result.release_id == str(RELEASE_ID)
    assert stub.requested["snapshot_id"] == UUID("66666666-6666-4666-8666-666666666666")
    assert "expected_agent_version" not in stub.requested
    assert len(cast(str, stub.requested["request_hash"])) == 64


@pytest.mark.asyncio
async def test_get_failed_release_uses_current_request_id_and_safe_error() -> None:
    stub = Stub(("agent:read",))
    stub.record = release(status="FAILED", error_code="BUNDLE_SCAN_FAILED")
    service = ReleaseManagementService(stub, cast(ReleaseStore, stub))

    result = await service.get_release(
        principal(), release_id=str(RELEASE_ID), metadata=METADATA
    )

    assert result.status == "FAILED"
    assert result.error is not None
    assert result.error.code == "BUNDLE_SCAN_FAILED"
    assert result.error.request_id == METADATA.request_id
