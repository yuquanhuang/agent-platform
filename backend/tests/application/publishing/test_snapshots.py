"""Snapshot compilation authorization and request-boundary tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    RequestMetadata,
    SnapshotCompilationService,
    SnapshotCompilationStore,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    AgentSnapshotRecord,
    AgentVersionRecord,
    SnapshotPublicationRecord,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
SNAPSHOT_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 7, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-snapshot", trace_id="trace-snapshot")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="publisher",
        display_name="Publisher",
        auth_time=NOW,
    )


def tenant_access(*permissions: str) -> TenantAccess:
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


class ResolverStub:
    def __init__(self, access: TenantAccess) -> None:
        self.access = access

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return self.access


class StoreStub:
    request_hash: str | None = None

    async def compile_snapshot(self, context: TenantContext, **kwargs: object):
        self.request_hash = cast(str, kwargs["request_hash"])
        return SnapshotPublicationRecord(
            version=AgentVersionRecord(
                id=VERSION_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                version_no=1,
                created_from_version_id=None,
                release_note="Initial release",
                created_at=NOW,
                created_by=ACTOR_ID,
            ),
            snapshot=AgentSnapshotRecord(
                id=SNAPSHOT_ID,
                tenant_id=TENANT_ID,
                agent_version_id=VERSION_ID,
                schema_version="agent-snapshot/v1",
                content={},
                content_hash="sha256:" + "a" * 64,
                compiler_input_hash="sha256:" + "b" * 64,
                created_at=NOW,
                created_by=ACTOR_ID,
            ),
        )


@pytest.mark.asyncio
async def test_service_authorizes_and_fingerprints_snapshot_compilation() -> None:
    store = StoreStub()
    service = SnapshotCompilationService(
        ResolverStub(tenant_access("agent:publish")),
        cast(SnapshotCompilationStore, store),
    )

    result = await service.compile_snapshot(
        principal(),
        agent_id=str(AGENT_ID),
        expected_draft_resource_version=2,
        release_note="Initial release",
        idempotency_key="snapshot-1",
        metadata=METADATA,
    )

    assert result.snapshot.id == SNAPSHOT_ID
    assert store.request_hash is not None
    assert len(store.request_hash) == 64


@pytest.mark.asyncio
async def test_service_rejects_missing_publish_permission() -> None:
    service = SnapshotCompilationService(
        ResolverStub(tenant_access("agent:read")),
        cast(SnapshotCompilationStore, StoreStub()),
    )

    with pytest.raises(PlatformError) as denied:
        await service.compile_snapshot(
            principal(),
            agent_id=str(AGENT_ID),
            expected_draft_resource_version=2,
            release_note="Initial release",
            idempotency_key="snapshot-1",
            metadata=METADATA,
        )

    assert denied.value.code == "PERMISSION_DENIED"
