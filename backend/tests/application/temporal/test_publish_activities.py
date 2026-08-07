"""Release Activity coordination and pre-activation failure protection tests."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.bundles import AgentScopeBundleCompilationService
from packages.application.publishing import SnapshotCompilationStore
from packages.application.temporal import (
    BundleArtifactPublisher,
    BundleSecurityScanner,
    ConfiguredReleaseRuntimeValidator,
    PublishedBundleArtifact,
    ReleaseDeploymentActivator,
    ReleaseSmokeTester,
    ReleaseWorkflowActivities,
    ReleaseWorkflowStore,
    RuntimeTargetReleaseConfig,
)
from packages.contracts.public import TenantContext
from packages.contracts.temporal import PublishAgentWorkflowInput
from packages.domain.public import (
    AgentSnapshotRecord,
    AgentVersionRecord,
    CompiledRuntimeBundle,
    ReleaseRecord,
    ReleaseStatus,
    RuntimeBundleRecord,
    RuntimeBundleScanStatus,
    SnapshotPublicationRecord,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
RELEASE_ID = UUID("44444444-4444-4444-8444-444444444444")
OPERATION_ID = UUID("55555555-5555-4555-8555-555555555555")
VERSION_ID = UUID("66666666-6666-4666-8666-666666666666")
SNAPSHOT_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 7, tzinfo=UTC)


def workflow_input(*, smoke: bool = True, activate: bool = False):
    return PublishAgentWorkflowInput(
        tenant_id=TENANT_ID,
        release_id=RELEASE_ID,
        operation_id=OPERATION_ID,
        agent_id=AGENT_ID,
        expected_agent_version=3,
        runtime_targets=["rt_agentscope_default"],
        run_smoke_test=smoke,
        activate_on_success=activate,
        request_id="req-release",
        trace_id="trace-release",
    )


def release() -> ReleaseRecord:
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


class StoreStub:
    def __init__(self) -> None:
        self.release = release()
        self.bundles: tuple[RuntimeBundleRecord, ...] = ()
        self.failed: str | None = None

    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None:
        return self.release

    async def transition_release(self, context: TenantContext, **kwargs: object):
        target = cast(str, kwargs["target_status"])
        snapshot_id = cast(UUID | None, kwargs.get("snapshot_id"))
        self.release = replace(
            self.release,
            status=cast(ReleaseStatus, target),
            snapshot_id=snapshot_id or self.release.snapshot_id,
            started_at=NOW,
        )
        return self.release

    async def store_runtime_bundle(
        self, context: TenantContext, *, record: RuntimeBundleRecord
    ) -> RuntimeBundleRecord:
        self.bundles = (record,)
        return record

    async def list_runtime_bundles(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> tuple[RuntimeBundleRecord, ...]:
        return self.bundles

    async def set_bundle_scan_status(
        self,
        context: TenantContext,
        *,
        bundle_id: UUID,
        status: RuntimeBundleScanStatus,
    ) -> None:
        self.bundles = (replace(self.bundles[0], scan_status="PASSED"),)

    async def complete_release(self, context: TenantContext, **kwargs: object):
        self.release = replace(
            self.release,
            status="SUCCEEDED",
            deployment_ids=cast(tuple[UUID, ...], kwargs["deployment_ids"]),
            finished_at=NOW,
        )
        return self.release

    async def fail_release(self, context: TenantContext, **kwargs: object):
        self.failed = cast(str, kwargs["error_code"])
        self.release = replace(self.release, status="FAILED", error_code=self.failed)
        return self.release


class SnapshotStub:
    def __init__(self) -> None:
        self.compile_calls = 0

    async def get_snapshot(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> AgentSnapshotRecord | None:
        return self._snapshot() if snapshot_id == SNAPSHOT_ID else None

    async def get_version(
        self, context: TenantContext, *, agent_version_id: UUID
    ) -> AgentVersionRecord | None:
        return self._version() if agent_version_id == VERSION_ID else None

    async def compile_snapshot(self, context: TenantContext, **kwargs: object):
        self.compile_calls += 1
        return SnapshotPublicationRecord(
            version=self._version(),
            snapshot=self._snapshot(),
        )

    @staticmethod
    def _version() -> AgentVersionRecord:
        return AgentVersionRecord(
            id=VERSION_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            version_no=1,
            created_from_version_id=None,
            release_note="Release",
            created_at=NOW,
            created_by=ACTOR_ID,
        )

    @staticmethod
    def _snapshot() -> AgentSnapshotRecord:
        return AgentSnapshotRecord(
            id=SNAPSHOT_ID,
            tenant_id=TENANT_ID,
            agent_version_id=VERSION_ID,
            schema_version="agent-snapshot/v1",
            content={},
            content_hash="sha256:" + "a" * 64,
            compiler_input_hash="sha256:" + "b" * 64,
            created_at=NOW,
            created_by=ACTOR_ID,
        )


class CompilerStub:
    async def compile_bundle(self, context: TenantContext, *, snapshot_id: UUID):
        return CompiledRuntimeBundle(
            bundle_id="bundle_" + "c" * 64,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            snapshot_id=SNAPSHOT_ID,
            runtime_type="agentscope",
            compiler_name="AgentScopeBundleCompiler",
            compiler_version="1.0.0",
            manifest={"schema_version": "1.0"},
            files=(),
            content_hash="sha256:" + "c" * 64,
            created_at=NOW,
        )


class Dependencies:
    def __init__(self, activation_ids: tuple[UUID, ...] = ()) -> None:
        self.smoke_calls = 0
        self.activation_calls = 0
        self.activation_ids = activation_ids

    async def publish(self, context: TenantContext, **kwargs: object):
        return PublishedBundleArtifact(
            object_uri="s3://bundles/bundle.tar",
            signature_ref="sigstore://bundle",
            sbom_ref="s3://bundles/bundle.spdx.json",
        )

    async def scan(self, context: TenantContext, **kwargs: object) -> None:
        return None

    async def run(self, context: TenantContext, **kwargs: object) -> None:
        self.smoke_calls += 1

    async def activate(self, context: TenantContext, **kwargs: object):
        self.activation_calls += 1
        return self.activation_ids


@pytest.mark.asyncio
async def test_release_activities_reuse_snapshot_and_bundle_then_skip_activation() -> (
    None
):
    store = StoreStub()
    dependencies = Dependencies()
    validator = ConfiguredReleaseRuntimeValidator(
        {
            "rt_agentscope_default": RuntimeTargetReleaseConfig(
                runtime_type="agentscope",
                image_digest="registry.example/agentscope@sha256:" + "d" * 64,
            )
        }
    )
    activities = ReleaseWorkflowActivities(
        cast(ReleaseWorkflowStore, store),
        cast(SnapshotCompilationStore, SnapshotStub()),
        cast(AgentScopeBundleCompilationService, CompilerStub()),
        validator,
        cast(BundleArtifactPublisher, dependencies),
        cast(BundleSecurityScanner, dependencies),
        cast(ReleaseSmokeTester, dependencies),
        cast(ReleaseDeploymentActivator, dependencies),
    )
    input = workflow_input()

    await activities.validate_release(input)
    await activities.compile_release_bundles(input)
    await activities.scan_release_bundles(input)
    await activities.smoke_test_release(input)
    await activities.activate_release(input)

    assert store.release.status == "SUCCEEDED"
    assert store.release.snapshot_id == SNAPSHOT_ID
    assert store.bundles[0].scan_status == "PASSED"
    assert dependencies.smoke_calls == 1
    assert dependencies.activation_calls == 0
    assert store.release.deployment_ids == ()


@pytest.mark.asyncio
async def test_activate_release_persists_adapter_deployment_ids() -> None:
    deployment_id = UUID("88888888-8888-4888-8888-888888888888")
    store = StoreStub()
    store.release = replace(
        release(),
        status="ACTIVATING",
        snapshot_id=SNAPSHOT_ID,
        activate_on_success=True,
    )
    store.bundles = (
        RuntimeBundleRecord(
            id=UUID("99999999-9999-4999-8999-999999999999"),
            tenant_id=TENANT_ID,
            snapshot_id=SNAPSHOT_ID,
            runtime_type="agentscope",
            compiler_name="AgentScopeBundleCompiler",
            compiler_version="1.0.0",
            manifest_schema_version="1.0",
            manifest={"schema_version": "1.0"},
            content_hash="sha256:" + "a" * 64,
            object_uri="s3://bundles/bundle.tar",
            size_bytes=100,
            signature_ref="sigstore://bundle",
            sbom_ref="s3://bundles/bundle.spdx.json",
            scan_status="PASSED",
            created_at=NOW,
        ),
    )
    dependencies = Dependencies((deployment_id,))
    activities = ReleaseWorkflowActivities(
        cast(ReleaseWorkflowStore, store),
        cast(SnapshotCompilationStore, SnapshotStub()),
        cast(AgentScopeBundleCompilationService, CompilerStub()),
        ConfiguredReleaseRuntimeValidator({}),
        cast(BundleArtifactPublisher, dependencies),
        cast(BundleSecurityScanner, dependencies),
        cast(ReleaseSmokeTester, dependencies),
        cast(ReleaseDeploymentActivator, dependencies),
    )

    await activities.activate_release(workflow_input(activate=True))

    assert dependencies.activation_calls == 1
    assert store.release.status == "SUCCEEDED"
    assert store.release.deployment_ids == (deployment_id,)


@pytest.mark.asyncio
async def test_rollback_validation_reuses_historical_snapshot_without_compiling_draft() -> (
    None
):
    store = StoreStub()
    store.release = replace(
        release(),
        release_kind="ROLLBACK",
        expected_agent_version=None,
        requested_snapshot_id=SNAPSHOT_ID,
    )
    snapshots = SnapshotStub()
    dependencies = Dependencies()
    activities = ReleaseWorkflowActivities(
        cast(ReleaseWorkflowStore, store),
        cast(SnapshotCompilationStore, snapshots),
        cast(AgentScopeBundleCompilationService, CompilerStub()),
        ConfiguredReleaseRuntimeValidator(
            {
                "rt_agentscope_default": RuntimeTargetReleaseConfig(
                    runtime_type="agentscope",
                    image_digest="registry.example/agentscope@sha256:" + "d" * 64,
                )
            }
        ),
        cast(BundleArtifactPublisher, dependencies),
        cast(BundleSecurityScanner, dependencies),
        cast(ReleaseSmokeTester, dependencies),
        cast(ReleaseDeploymentActivator, dependencies),
    )

    await activities.validate_release(
        PublishAgentWorkflowInput(
            tenant_id=TENANT_ID,
            release_id=RELEASE_ID,
            operation_id=OPERATION_ID,
            agent_id=AGENT_ID,
            release_kind="ROLLBACK",
            expected_agent_version=None,
            requested_snapshot_id=SNAPSHOT_ID,
            runtime_targets=["rt_agentscope_default"],
            run_smoke_test=True,
            activate_on_success=True,
            request_id="req-rollback",
            trace_id="trace-rollback",
        )
    )

    assert snapshots.compile_calls == 0
    assert store.release.status == "COMPILING"
    assert store.release.snapshot_id == SNAPSHOT_ID
