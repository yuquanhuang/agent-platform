"""Frozen Agent Release route registration tests."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    DeploymentManagementService,
    DeploymentStore,
    PublicationQueryService,
    PublicationQueryStore,
    ReleaseManagementService,
    ReleaseStore,
    RequestMetadata,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    AgentSnapshotRecord,
    AgentVersionRecord,
    AgentVersionSnapshotRecord,
    DeploymentRecord,
    MutationOutcome,
    PublishPreviewRecord,
    PublishPreviewTargetRecord,
    ReleaseRecord,
    ResolvedPreviewBinding,
    SnapshotChangeRecord,
    TenantAccess,
)
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
RELEASE_ID = UUID("44444444-4444-4444-8444-444444444444")
OPERATION_ID = UUID("55555555-5555-4555-8555-555555555555")
DEPLOYMENT_ID = UUID("66666666-6666-4666-8666-666666666666")
SNAPSHOT_ID = UUID("77777777-7777-4777-8777-777777777777")
BUNDLE_ID = UUID("88888888-8888-4888-8888-888888888888")
VERSION_ID = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2026, 8, 7, tzinfo=UTC)


class ReleaseStub:
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
            permissions=frozenset({"agent:publish", "agent:read"}),
        )

    async def request_release(self, context: TenantContext, **kwargs: object):
        return MutationOutcome(value=self._record())

    async def request_rollback(self, context: TenantContext, **kwargs: object):
        return MutationOutcome(
            value=replace(
                self._record(),
                release_kind="ROLLBACK",
                expected_agent_version=None,
                requested_snapshot_id=SNAPSHOT_ID,
            )
        )

    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None:
        return self._record() if release_id == RELEASE_ID else None

    async def get_deployment(
        self, context: TenantContext, *, deployment_id: UUID
    ) -> DeploymentRecord | None:
        if deployment_id != DEPLOYMENT_ID:
            return None
        return DeploymentRecord(
            id=DEPLOYMENT_ID,
            tenant_id=TENANT_ID,
            release_id=RELEASE_ID,
            agent_id=AGENT_ID,
            snapshot_id=SNAPSHOT_ID,
            bundle_id=BUNDLE_ID,
            runtime_target_id="rt_agentscope_default",
            status="ACTIVE",
            compatibility_hash="sha256:" + "a" * 64,
            activation_fencing_token=1,
            created_at=NOW,
            activated_at=NOW,
            retired_at=None,
        )

    async def list_agent_versions(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[list[AgentVersionSnapshotRecord], None] | None:
        return ([self._version()], None) if kwargs["agent_id"] == AGENT_ID else None

    async def get_agent_version(
        self, context: TenantContext, *, agent_id: UUID, version_id: UUID
    ) -> AgentVersionSnapshotRecord | None:
        return (
            self._version()
            if (agent_id, version_id) == (AGENT_ID, VERSION_ID)
            else None
        )

    async def diff_agent_snapshots(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[SnapshotChangeRecord, ...] | None:
        return (
            SnapshotChangeRecord(
                category="model",
                path="/bindings/0/version_id",
                change_type="changed",
                before="version-old",
                after="version-new",
            ),
        )

    async def preview_agent_publish(
        self, context: TenantContext, **kwargs: object
    ) -> PublishPreviewRecord | None:
        if kwargs["agent_id"] != AGENT_ID:
            return None
        return PublishPreviewRecord(
            agent_id=AGENT_ID,
            expected_agent_version=3,
            preview_snapshot_hash="sha256:" + "c" * 64,
            resolved_bindings=(
                ResolvedPreviewBinding(
                    resource_type="model",
                    resource_id=BUNDLE_ID,
                    version_id=VERSION_ID,
                    version_no=1,
                    content_hash="sha256:" + "d" * 64,
                    binding_role="primary",
                ),
            ),
            targets=(
                PublishPreviewTargetRecord(
                    runtime_target_id="rt_agentscope_default",
                    current_deployment_id=DEPLOYMENT_ID,
                    current_snapshot_id=SNAPSHOT_ID,
                    changes=(
                        SnapshotChangeRecord(
                            category="model",
                            path="/bindings/0/version_id",
                            change_type="changed",
                            before="version-old",
                            after="version-new",
                        ),
                    ),
                ),
            ),
            ready_to_publish=True,
        )

    @staticmethod
    def _version() -> AgentVersionSnapshotRecord:
        return AgentVersionSnapshotRecord(
            version=AgentVersionRecord(
                id=VERSION_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                version_no=1,
                created_from_version_id=None,
                release_note="Initial",
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

    @staticmethod
    def _record() -> ReleaseRecord:
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
            activate_on_success=False,
            status="REQUESTED",
            workflow_id=f"publish/{TENANT_ID}/{RELEASE_ID}",
            snapshot_id=None,
            deployment_ids=(),
            error_code=None,
            error_detail=None,
            created_at=NOW,
            started_at=None,
            finished_at=None,
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = ReleaseStub()
    service = ReleaseManagementService(stub, cast(ReleaseStore, stub))
    deployment_service = DeploymentManagementService(stub, cast(DeploymentStore, stub))
    query_service = PublicationQueryService(stub, cast(PublicationQueryStore, stub))
    return create_app(
        settings,
        identity_provider=provider,
        release_service=service,
        deployment_service=deployment_service,
        publication_query_service=query_service,
    )


@pytest.mark.asyncio
async def test_publish_and_get_release_use_frozen_paths_and_shapes() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        accepted = await client.post(
            f"/api/v1/agents/{AGENT_ID}/publish",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "release-idempotency",
                "X-Request-ID": "req-release",
            },
            json={
                "expected_agent_version": 3,
                "runtime_targets": ["rt_agentscope_default"],
                "release_note": "Release",
                "activate_on_success": False,
            },
        )
        detail = await client.get(
            f"/api/v1/releases/{RELEASE_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-release"},
        )
        deployment = await client.get(
            f"/api/v1/deployments/{DEPLOYMENT_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-release"},
        )

    assert accepted.status_code == 202
    assert accepted.json()["status_url"] == f"/api/v1/releases/{RELEASE_ID}"
    assert detail.status_code == 200
    assert detail.json()["workflow_id"] == f"publish/{TENANT_ID}/{RELEASE_ID}"
    assert deployment.status_code == 200
    assert deployment.json()["id"] == str(DEPLOYMENT_ID)
    assert deployment.json()["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_rollback_uses_frozen_path_and_acceptance_shape() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        accepted = await client.post(
            f"/api/v1/agents/{AGENT_ID}/rollback",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "rollback-idempotency",
                "X-Request-ID": "req-rollback",
            },
            json={
                "snapshot_id": str(SNAPSHOT_ID),
                "runtime_targets": ["rt_agentscope_default"],
                "release_note": "Rollback to v1",
            },
        )

    assert accepted.status_code == 202
    assert accepted.json()["release_id"] == str(RELEASE_ID)
    assert accepted.json()["status_url"] == f"/api/v1/releases/{RELEASE_ID}"


@pytest.mark.asyncio
async def test_preview_version_history_and_diff_use_frozen_paths() -> None:
    transport = httpx.ASGITransport(app=build_app())
    headers = {"Authorization": "Bearer mock", "X-Request-ID": "req-preview"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        preview = await client.post(
            f"/api/v1/agents/{AGENT_ID}/publish-preview",
            headers=headers,
            json={
                "expected_agent_version": 3,
                "runtime_targets": ["rt_agentscope_default"],
            },
        )
        versions = await client.get(
            f"/api/v1/agents/{AGENT_ID}/versions", headers=headers
        )
        version = await client.get(
            f"/api/v1/agents/{AGENT_ID}/versions/{VERSION_ID}", headers=headers
        )
        diff = await client.get(
            f"/api/v1/agents/{AGENT_ID}/diff",
            headers=headers,
            params={
                "from_snapshot_id": str(SNAPSHOT_ID),
                "to_snapshot_id": str(SNAPSHOT_ID),
            },
        )

    assert preview.status_code == 200
    assert preview.json()["ready_to_publish"] is True
    assert preview.json()["targets"][0]["current_deployment_id"] == str(DEPLOYMENT_ID)
    assert versions.status_code == 200
    assert versions.json()["items"][0]["snapshot_id"] == str(SNAPSHOT_ID)
    assert version.status_code == 200
    assert version.json()["id"] == str(VERSION_ID)
    assert diff.status_code == 200
    assert diff.json()["changes"][0]["category"] == "model"
