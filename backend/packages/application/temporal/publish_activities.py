"""Release Activity implementations composed from existing publication boundaries."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue
from temporalio import activity
from temporalio.exceptions import ApplicationError

from packages.application.bundles import AgentScopeBundleCompilationService
from packages.application.metadata import RequestMetadata
from packages.application.publishing import SnapshotCompilationStore
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.errors import PlatformError
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import (
    PublishAgentWorkflowInput,
    PublishReleaseFailureInput,
)
from packages.domain.public import (
    BundleCompilationError,
    CompiledRuntimeBundle,
    ReleaseRecord,
    ReleaseStatus,
    RuntimeBundleRecord,
    RuntimeBundleScanStatus,
)

_DIGEST_RE = re.compile(r"^.+@sha256:[a-f0-9]{64}$")


class ReleaseWorkflowStore(Protocol):
    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None: ...

    async def transition_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        expected_status: ReleaseStatus,
        target_status: ReleaseStatus,
        snapshot_id: UUID | None = None,
    ) -> ReleaseRecord: ...

    async def store_runtime_bundle(
        self, context: TenantContext, *, record: RuntimeBundleRecord
    ) -> RuntimeBundleRecord: ...

    async def list_runtime_bundles(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> tuple[RuntimeBundleRecord, ...]: ...

    async def set_bundle_scan_status(
        self,
        context: TenantContext,
        *,
        bundle_id: UUID,
        status: RuntimeBundleScanStatus,
    ) -> None: ...

    async def complete_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        expected_status: ReleaseStatus,
        deployment_ids: tuple[UUID, ...],
    ) -> ReleaseRecord: ...

    async def fail_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        error_code: str,
        error_detail: dict[str, JsonValue],
    ) -> ReleaseRecord: ...


@dataclass(frozen=True, slots=True)
class RuntimeTargetReleaseConfig:
    runtime_type: str
    image_digest: str | None


class ReleaseRuntimeValidator(Protocol):
    async def validate(
        self, context: TenantContext, release: ReleaseRecord
    ) -> tuple[str, ...]: ...


class ConfiguredReleaseRuntimeValidator:
    """Validate target identity and require a Registry manifest digest."""

    def __init__(self, targets: dict[str, RuntimeTargetReleaseConfig]) -> None:
        self._targets = dict(targets)

    async def validate(
        self, context: TenantContext, release: ReleaseRecord
    ) -> tuple[str, ...]:
        del context
        runtime_types: list[str] = []
        for target in release.runtime_targets:
            config = self._targets.get(target)
            if config is None:
                raise ReleaseStageError(
                    "RUNTIME_TARGET_NOT_FOUND",
                    "A requested Runtime Target is unavailable.",
                )
            if config.image_digest is None or not _DIGEST_RE.fullmatch(
                config.image_digest
            ):
                raise ReleaseStageError(
                    "RUNTIME_IMAGE_DIGEST_UNAVAILABLE",
                    "The Runtime image Registry manifest digest is unavailable.",
                )
            runtime_types.append(config.runtime_type)
        return tuple(runtime_types)


@dataclass(frozen=True, slots=True)
class PublishedBundleArtifact:
    object_uri: str
    signature_ref: str
    sbom_ref: str


class BundleArtifactPublisher(Protocol):
    """Persist exact Bundle bytes, produce SBOM, and sign by content hash."""

    async def publish(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        bundle: CompiledRuntimeBundle,
    ) -> PublishedBundleArtifact: ...


class BundleSecurityScanner(Protocol):
    async def scan(
        self, context: TenantContext, *, bundle: RuntimeBundleRecord
    ) -> None: ...


class ReleaseSmokeTester(Protocol):
    async def run(
        self,
        context: TenantContext,
        *,
        release: ReleaseRecord,
        bundles: tuple[RuntimeBundleRecord, ...],
    ) -> None: ...


class ReleaseDeploymentActivator(Protocol):
    """AP-E2-004 implements the fencing and ACTIVE Deployment transaction."""

    async def activate(
        self,
        context: TenantContext,
        *,
        release: ReleaseRecord,
        bundles: tuple[RuntimeBundleRecord, ...],
    ) -> tuple[UUID, ...]: ...


class ReleaseStageError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class ReleaseWorkflowActivities:
    """Activity boundary with idempotent stage writes and fail-closed adapters."""

    def __init__(
        self,
        store: ReleaseWorkflowStore,
        snapshot_store: SnapshotCompilationStore,
        bundle_compiler: AgentScopeBundleCompilationService,
        runtime_validator: ReleaseRuntimeValidator,
        artifact_publisher: BundleArtifactPublisher,
        scanner: BundleSecurityScanner,
        smoke_tester: ReleaseSmokeTester,
        activator: ReleaseDeploymentActivator,
    ) -> None:
        self._store = store
        self._snapshots = snapshot_store
        self._bundle_compiler = bundle_compiler
        self._runtime_validator = runtime_validator
        self._artifact_publisher = artifact_publisher
        self._scanner = scanner
        self._smoke_tester = smoke_tester
        self._activator = activator

    @activity.defn(name="validate_release_v1")
    async def validate_release(self, input: PublishAgentWorkflowInput) -> None:
        try:
            context = _context(input)
            release = await self._required_release(context, input.release_id)
            release = await self._store.transition_release(
                context,
                release_id=release.id,
                expected_status="REQUESTED",
                target_status="VALIDATING",
            )
            runtime_types = await self._runtime_validator.validate(context, release)
            if not runtime_types or set(runtime_types) != {"agentscope"}:
                raise ReleaseStageError(
                    "UNSUPPORTED_RUNTIME_TARGET",
                    "AP-E2-003 supports only AgentScope Runtime Targets.",
                )
            snapshot_id = await self._resolve_release_snapshot(context, release, input)
            await self._store.transition_release(
                context,
                release_id=release.id,
                expected_status="VALIDATING",
                target_status="COMPILING",
                snapshot_id=snapshot_id,
            )
        except (
            ApplicationError,
            BundleCompilationError,
            PlatformError,
            ReleaseStageError,
        ) as error:
            _raise_activity_error(error)

    @activity.defn(name="compile_release_bundles_v1")
    async def compile_release_bundles(self, input: PublishAgentWorkflowInput) -> None:
        try:
            context = _context(input)
            release = await self._required_release(context, input.release_id)
            if release.snapshot_id is None:
                raise ReleaseStageError(
                    "RELEASE_SNAPSHOT_REQUIRED",
                    "Release Snapshot compilation did not complete.",
                )
            bundle = await self._bundle_compiler.compile_bundle(
                context, snapshot_id=release.snapshot_id
            )
            _heartbeat(release.id, "compiled")
            artifact = await self._artifact_publisher.publish(
                context, release_id=release.id, bundle=bundle
            )
            if not artifact.signature_ref or not artifact.sbom_ref:
                raise ReleaseStageError(
                    "BUNDLE_SUPPLY_CHAIN_METADATA_REQUIRED",
                    "Bundle signature and SBOM references are required.",
                )
            record = RuntimeBundleRecord(
                id=uuid5(
                    NAMESPACE_URL,
                    f"runtime-bundle/{bundle.tenant_id}/{bundle.bundle_id}",
                ),
                tenant_id=bundle.tenant_id,
                snapshot_id=bundle.snapshot_id,
                runtime_type=bundle.runtime_type,
                compiler_name=bundle.compiler_name,
                compiler_version=bundle.compiler_version,
                manifest_schema_version=str(bundle.manifest["schema_version"]),
                manifest=bundle.manifest,
                content_hash=bundle.content_hash,
                object_uri=artifact.object_uri,
                size_bytes=bundle.size_bytes,
                signature_ref=artifact.signature_ref,
                sbom_ref=artifact.sbom_ref,
                scan_status="PENDING",
                created_at=bundle.created_at,
            )
            await self._store.store_runtime_bundle(context, record=record)
            await self._store.transition_release(
                context,
                release_id=release.id,
                expected_status="COMPILING",
                target_status="SCANNING",
            )
        except (
            ApplicationError,
            BundleCompilationError,
            PlatformError,
            ReleaseStageError,
        ) as error:
            _raise_activity_error(error)

    @activity.defn(name="scan_release_bundles_v1")
    async def scan_release_bundles(self, input: PublishAgentWorkflowInput) -> None:
        try:
            context = _context(input)
            release = await self._required_release(context, input.release_id)
            bundles = await self._required_bundles(context, release)
            for bundle in bundles:
                if bundle.scan_status == "PASSED":
                    continue
                await self._scanner.scan(context, bundle=bundle)
                await self._store.set_bundle_scan_status(
                    context, bundle_id=bundle.id, status="PASSED"
                )
            await self._store.transition_release(
                context,
                release_id=release.id,
                expected_status="SCANNING",
                target_status=(
                    "SMOKE_TESTING" if input.run_smoke_test else "ACTIVATING"
                ),
            )
        except (
            ApplicationError,
            BundleCompilationError,
            PlatformError,
            ReleaseStageError,
        ) as error:
            _raise_activity_error(error)

    @activity.defn(name="smoke_test_release_v1")
    async def smoke_test_release(self, input: PublishAgentWorkflowInput) -> None:
        try:
            context = _context(input)
            release = await self._required_release(context, input.release_id)
            bundles = await self._required_bundles(context, release)
            await self._smoke_tester.run(context, release=release, bundles=bundles)
            await self._store.transition_release(
                context,
                release_id=release.id,
                expected_status="SMOKE_TESTING",
                target_status="ACTIVATING",
            )
        except (
            ApplicationError,
            BundleCompilationError,
            PlatformError,
            ReleaseStageError,
        ) as error:
            _raise_activity_error(error)

    @activity.defn(name="activate_release_v1")
    async def activate_release(self, input: PublishAgentWorkflowInput) -> None:
        try:
            context = _context(input)
            release = await self._required_release(context, input.release_id)
            bundles = await self._required_bundles(context, release)
            deployment_ids: tuple[UUID, ...] = ()
            if release.activate_on_success:
                deployment_ids = await self._activator.activate(
                    context, release=release, bundles=bundles
                )
                if len(deployment_ids) != len(release.runtime_targets):
                    raise ReleaseStageError(
                        "DEPLOYMENT_ACTIVATION_INCOMPLETE",
                        "Not every Runtime Target produced a Deployment.",
                    )
            await self._store.complete_release(
                context,
                release_id=release.id,
                expected_status="ACTIVATING",
                deployment_ids=deployment_ids,
            )
        except (
            ApplicationError,
            BundleCompilationError,
            PlatformError,
            ReleaseStageError,
        ) as error:
            _raise_activity_error(error)

    @activity.defn(name="fail_release_v1")
    async def fail_release(self, input: PublishReleaseFailureInput) -> None:
        context = _context(input)
        await self._store.fail_release(
            context,
            release_id=input.release_id,
            error_code=input.error_code,
            error_detail={"message": input.error_message},
        )

    async def _required_release(
        self, context: TenantContext, release_id: UUID
    ) -> ReleaseRecord:
        release = await self._store.get_release(context, release_id=release_id)
        if release is None:
            raise ReleaseStageError("RELEASE_NOT_FOUND", "Release is unavailable.")
        return release

    async def _resolve_release_snapshot(
        self,
        context: TenantContext,
        release: ReleaseRecord,
        input: PublishAgentWorkflowInput,
    ) -> UUID:
        if release.release_kind == "ROLLBACK":
            if release.requested_snapshot_id is None:
                raise ReleaseStageError(
                    "ROLLBACK_SNAPSHOT_REQUIRED",
                    "Rollback Release does not identify a historical Snapshot.",
                )
            snapshot = await self._snapshots.get_snapshot(
                context, snapshot_id=release.requested_snapshot_id
            )
            if snapshot is None:
                raise ReleaseStageError(
                    "ROLLBACK_SNAPSHOT_NOT_FOUND",
                    "The historical Snapshot is unavailable.",
                )
            version = await self._snapshots.get_version(
                context, agent_version_id=snapshot.agent_version_id
            )
            if version is None or version.agent_id != release.agent_id:
                raise ReleaseStageError(
                    "ROLLBACK_SNAPSHOT_NOT_FOUND",
                    "The historical Snapshot is unavailable.",
                )
            return snapshot.id
        if release.expected_agent_version is None:
            raise ReleaseStageError(
                "PUBLISH_DRAFT_VERSION_REQUIRED",
                "Publish Release does not identify an Agent Draft version.",
            )
        publication = await self._snapshots.compile_snapshot(
            context,
            actor_id=release.requested_by,
            agent_id=release.agent_id,
            expected_draft_resource_version=release.expected_agent_version,
            release_note=release.release_note,
            idempotency_key=f"release:{release.id}:snapshot",
            request_hash=canonical_request_hash(
                "agent.snapshot.compile",
                extra={
                    "agent_id": str(release.agent_id),
                    "expected_draft_resource_version": release.expected_agent_version,
                    "release_note": release.release_note,
                },
            ),
            metadata=_metadata(input),
        )
        if publication is None:
            raise ReleaseStageError(
                "AGENT_NOT_FOUND", "The Agent Draft is unavailable."
            )
        return publication.snapshot.id

    async def _required_bundles(
        self, context: TenantContext, release: ReleaseRecord
    ) -> tuple[RuntimeBundleRecord, ...]:
        if release.snapshot_id is None:
            raise ReleaseStageError(
                "RELEASE_SNAPSHOT_REQUIRED", "Release Snapshot is unavailable."
            )
        bundles = await self._store.list_runtime_bundles(
            context, snapshot_id=release.snapshot_id
        )
        if not bundles:
            raise ReleaseStageError(
                "RUNTIME_BUNDLE_REQUIRED", "Release Runtime Bundle is unavailable."
            )
        return bundles


def _context(input: PublishAgentWorkflowInput) -> TenantContext:
    return TenantContext(
        tenant_id=str(input.tenant_id),
        subject_type=SubjectType.SERVICE,
        subject_id=str(input.operation_id),
        auth_time=datetime.now(UTC),
        request_id=input.request_id,
        trace_id=input.trace_id,
    )


def _metadata(input: PublishAgentWorkflowInput) -> RequestMetadata:
    return RequestMetadata(request_id=input.request_id, trace_id=input.trace_id)


def _heartbeat(release_id: UUID, stage: str) -> None:
    try:
        activity.heartbeat({"release_id": str(release_id), "stage": stage})
    except RuntimeError:
        # Direct application tests intentionally execute Activities without a worker.
        return


def _raise_activity_error(error: Exception) -> None:
    if isinstance(error, ApplicationError):
        raise error
    if isinstance(error, ReleaseStageError):
        raise ApplicationError(
            error.message,
            type=error.code,
            non_retryable=not error.retryable,
        ) from error
    if isinstance(error, BundleCompilationError):
        raise ApplicationError(
            str(error), type=error.code, non_retryable=True
        ) from error
    if isinstance(error, PlatformError):
        raise ApplicationError(
            error.message,
            type=error.code,
            non_retryable=not error.retryable,
        ) from error
    raise ApplicationError(
        "The Release stage failed.",
        type="RELEASE_STAGE_FAILED",
        non_retryable=True,
    ) from error
