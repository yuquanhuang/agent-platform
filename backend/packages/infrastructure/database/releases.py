"""PostgreSQL persistence for Release workflow state and Runtime Bundles."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.metadata import RequestMetadata
from packages.application.publishing import publish_workflow_id
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
)
from packages.domain.public import (
    MutationOutcome,
    OutboxEvent,
    OutboxStatus,
    ReleaseKind,
    ReleaseRecord,
    ReleaseStatus,
    RuntimeBundleRecord,
    RuntimeBundleScanStatus,
    ensure_release_transition,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentDefinitionModel,
    AgentSnapshotModel,
    AgentVersionModel,
    AuditLogModel,
    OperationRecordModel,
    ReleaseModel,
    RuntimeBundleModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyReleaseStore:
    """Own short tenant transactions and compare-and-set Release transitions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def request_release(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_agent_version: int,
        runtime_targets: tuple[str, ...],
        release_note: str,
        run_smoke_test: bool,
        activate_on_success: bool,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ReleaseRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            idempotency_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.release.request",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            agent = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if agent is None:
                return None
            if agent.status == "DISABLED":
                raise resource_state_conflict("A disabled Agent cannot be published.")
            if agent.resource_version != expected_agent_version:
                raise resource_version_conflict()

            now = datetime.now(UTC)
            release_id = uuid4()
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.release",
                status="ACCEPTED",
                resource_type="release",
                resource_id=release_id,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            await session.flush()
            workflow_id = publish_workflow_id(tenant_id, release_id)
            release = ReleaseModel(
                id=release_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                requested_by=actor_id,
                operation_id=operation.id,
                release_kind="PUBLISH",
                expected_agent_version=expected_agent_version,
                requested_snapshot_id=None,
                runtime_targets_json=list(runtime_targets),
                release_note=release_note,
                run_smoke_test=run_smoke_test,
                activate_on_success=activate_on_success,
                status="REQUESTED",
                workflow_id=workflow_id,
                deployment_ids_json=[],
                created_at=now,
            )
            session.add(release)
            SqlAlchemyOutboxWriter(session, context).add(
                OutboxEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="release",
                    aggregate_id=release_id,
                    event_type="agent.release_requested.v1",
                    payload={
                        "tenant_id": str(tenant_id),
                        "release_id": str(release_id),
                        "operation_id": str(operation.id),
                        "agent_id": str(agent_id),
                        "release_kind": "PUBLISH",
                        "expected_agent_version": expected_agent_version,
                        "requested_snapshot_id": None,
                        "runtime_targets": list(runtime_targets),
                        "run_smoke_test": run_smoke_test,
                        "activate_on_success": activate_on_success,
                        "request_id": metadata.request_id,
                        "trace_id": metadata.trace_id,
                    },
                    payload_schema_version=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            await session.flush()
            result = _release_record(release)
            response_body: dict[str, object] = {
                "release_id": str(release_id),
                "workflow_id": workflow_id,
                "status": "REQUESTED",
                "status_url": f"/api/v1/releases/{release_id}",
            }
            await _audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.release.requested",
                release_id=release_id,
                metadata=metadata,
                change={
                    "agent_id": str(agent_id),
                    "operation_id": str(operation.id),
                    "runtime_targets": list(runtime_targets),
                    "run_smoke_test": run_smoke_test,
                    "activate_on_success": activate_on_success,
                },
            )
            await complete_idempotency(
                session,
                idempotency_id,
                response_status=202,
                response_body=response_body,
                response_etag=None,
                response_ref=str(release_id),
            )
            return MutationOutcome(value=result)

    async def request_rollback(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        snapshot_id: UUID,
        runtime_targets: tuple[str, ...],
        release_note: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ReleaseRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            idempotency_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.rollback.request",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            agent = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if agent is None:
                return None
            if agent.status == "DISABLED":
                raise resource_state_conflict("A disabled Agent cannot be rolled back.")
            historical_snapshot = await session.scalar(
                select(AgentSnapshotModel)
                .join(
                    AgentVersionModel,
                    (AgentVersionModel.tenant_id == AgentSnapshotModel.tenant_id)
                    & (AgentVersionModel.id == AgentSnapshotModel.agent_version_id),
                )
                .where(
                    AgentSnapshotModel.tenant_id == tenant_id,
                    AgentSnapshotModel.id == snapshot_id,
                    AgentVersionModel.agent_id == agent_id,
                )
            )
            if historical_snapshot is None:
                return None

            now = datetime.now(UTC)
            release_id = uuid4()
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="agent.rollback",
                status="ACCEPTED",
                resource_type="release",
                resource_id=release_id,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            await session.flush()
            workflow_id = publish_workflow_id(tenant_id, release_id)
            release = ReleaseModel(
                id=release_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                requested_by=actor_id,
                operation_id=operation.id,
                release_kind="ROLLBACK",
                expected_agent_version=None,
                requested_snapshot_id=snapshot_id,
                runtime_targets_json=list(runtime_targets),
                release_note=release_note,
                run_smoke_test=True,
                activate_on_success=True,
                status="REQUESTED",
                workflow_id=workflow_id,
                deployment_ids_json=[],
                created_at=now,
            )
            session.add(release)
            SqlAlchemyOutboxWriter(session, context).add(
                OutboxEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="release",
                    aggregate_id=release_id,
                    event_type="agent.release_requested.v1",
                    payload={
                        "tenant_id": str(tenant_id),
                        "release_id": str(release_id),
                        "operation_id": str(operation.id),
                        "agent_id": str(agent_id),
                        "release_kind": "ROLLBACK",
                        "expected_agent_version": None,
                        "requested_snapshot_id": str(snapshot_id),
                        "runtime_targets": list(runtime_targets),
                        "run_smoke_test": True,
                        "activate_on_success": True,
                        "request_id": metadata.request_id,
                        "trace_id": metadata.trace_id,
                    },
                    payload_schema_version=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            await session.flush()
            result = _release_record(release)
            response_body: dict[str, object] = {
                "release_id": str(release_id),
                "workflow_id": workflow_id,
                "status": "REQUESTED",
                "status_url": f"/api/v1/releases/{release_id}",
            }
            await _audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="agent.rollback.requested",
                release_id=release_id,
                metadata=metadata,
                change={
                    "agent_id": str(agent_id),
                    "operation_id": str(operation.id),
                    "snapshot_id": str(snapshot_id),
                    "runtime_targets": list(runtime_targets),
                    "run_smoke_test": True,
                    "activate_on_success": True,
                },
            )
            await complete_idempotency(
                session,
                idempotency_id,
                response_status=202,
                response_body=response_body,
                response_etag=None,
                response_ref=str(release_id),
            )
            return MutationOutcome(value=result)

    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(ReleaseModel).where(
                    ReleaseModel.tenant_id == UUID(context.tenant_id),
                    ReleaseModel.id == release_id,
                )
            )
            return _release_record(model) if model is not None else None

    async def transition_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        expected_status: ReleaseStatus,
        target_status: ReleaseStatus,
        snapshot_id: UUID | None = None,
    ) -> ReleaseRecord:
        ensure_release_transition(expected_status, target_status)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            release = await self._locked_release(
                unit_of_work.session, context, release_id
            )
            if release.status != expected_status:
                if release.status == target_status:
                    return _release_record(release)
                raise resource_state_conflict(
                    f"Release is {release.status}; expected {expected_status}."
                )
            now = datetime.now(UTC)
            release.status = target_status
            if snapshot_id is not None:
                release.snapshot_id = snapshot_id
            if release.started_at is None:
                release.started_at = now
            operation = await self._operation(unit_of_work.session, release)
            operation.status = "RUNNING"
            operation.updated_at = now
            return _release_record(release)

    async def store_runtime_bundle(
        self,
        context: TenantContext,
        *,
        record: RuntimeBundleRecord,
    ) -> RuntimeBundleRecord:
        if str(record.tenant_id) != context.tenant_id:
            raise ValueError("Runtime Bundle tenant does not match TenantContext")
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            existing = await session.scalar(
                select(RuntimeBundleModel).where(
                    RuntimeBundleModel.tenant_id == record.tenant_id,
                    RuntimeBundleModel.snapshot_id == record.snapshot_id,
                    RuntimeBundleModel.runtime_type == record.runtime_type,
                    RuntimeBundleModel.compiler_version == record.compiler_version,
                    RuntimeBundleModel.content_hash == record.content_hash,
                )
            )
            if existing is not None:
                current = _runtime_bundle_record(existing)
                if current.object_uri != record.object_uri:
                    raise resource_state_conflict(
                        "Runtime Bundle identity already points to another object."
                    )
                return current
            session.add(
                RuntimeBundleModel(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    snapshot_id=record.snapshot_id,
                    runtime_type=record.runtime_type,
                    compiler_name=record.compiler_name,
                    compiler_version=record.compiler_version,
                    manifest_schema_version=record.manifest_schema_version,
                    manifest_json=cast(dict[str, object], record.manifest),
                    content_hash=record.content_hash,
                    object_uri=record.object_uri,
                    size_bytes=record.size_bytes,
                    signature_ref=record.signature_ref,
                    sbom_ref=record.sbom_ref,
                    scan_status=record.scan_status,
                    created_at=record.created_at,
                )
            )
            await session.flush()
            return record

    async def list_runtime_bundles(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> tuple[RuntimeBundleRecord, ...]:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            rows = (
                await unit_of_work.session.scalars(
                    select(RuntimeBundleModel)
                    .where(
                        RuntimeBundleModel.tenant_id == UUID(context.tenant_id),
                        RuntimeBundleModel.snapshot_id == snapshot_id,
                    )
                    .order_by(RuntimeBundleModel.runtime_type, RuntimeBundleModel.id)
                )
            ).all()
            return tuple(_runtime_bundle_record(row) for row in rows)

    async def set_bundle_scan_status(
        self,
        context: TenantContext,
        *,
        bundle_id: UUID,
        status: RuntimeBundleScanStatus,
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            bundle = await unit_of_work.session.scalar(
                select(RuntimeBundleModel)
                .where(
                    RuntimeBundleModel.tenant_id == UUID(context.tenant_id),
                    RuntimeBundleModel.id == bundle_id,
                )
                .with_for_update()
            )
            if bundle is None:
                raise resource_state_conflict("Runtime Bundle is unavailable.")
            if bundle.scan_status == status:
                return
            if bundle.scan_status != "PENDING":
                raise resource_state_conflict(
                    "Runtime Bundle scan is already terminal."
                )
            bundle.scan_status = status

    async def complete_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        expected_status: ReleaseStatus,
        deployment_ids: tuple[UUID, ...],
    ) -> ReleaseRecord:
        ensure_release_transition(expected_status, "SUCCEEDED")
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            release = await self._locked_release(
                unit_of_work.session, context, release_id
            )
            if release.status == "SUCCEEDED":
                return _release_record(release)
            if release.status != expected_status:
                raise resource_state_conflict(
                    "Release cannot be finalized from this state."
                )
            now = datetime.now(UTC)
            release.status = "SUCCEEDED"
            release.deployment_ids_json = [str(item) for item in deployment_ids]
            release.finished_at = now
            operation = await self._operation(unit_of_work.session, release)
            operation.status = "SUCCEEDED"
            operation.result_json = {
                "release_id": str(release.id),
                "snapshot_id": str(release.snapshot_id),
                "deployment_ids": release.deployment_ids_json,
            }
            operation.updated_at = now
            operation.finished_at = now
            return _release_record(release)

    async def fail_release(
        self,
        context: TenantContext,
        *,
        release_id: UUID,
        error_code: str,
        error_detail: dict[str, JsonValue],
    ) -> ReleaseRecord:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            release = await self._locked_release(
                unit_of_work.session, context, release_id
            )
            if release.status == "FAILED":
                return _release_record(release)
            if release.status in {"SUCCEEDED", "CANCELLED"}:
                raise resource_state_conflict("A terminal Release cannot be failed.")
            if release.status == "REQUESTED":
                release.status = "VALIDATING"
            ensure_release_transition(cast(ReleaseStatus, release.status), "FAILED")
            now = datetime.now(UTC)
            release.status = "FAILED"
            release.error_code = error_code
            release.error_detail_json = cast(dict[str, object], error_detail)
            release.finished_at = now
            operation = await self._operation(unit_of_work.session, release)
            operation.status = "FAILED"
            operation.error_json = {
                "code": error_code,
                "message": str(error_detail.get("message", "Release workflow failed.")),
                "retryable": False,
            }
            operation.updated_at = now
            operation.finished_at = now
            return _release_record(release)

    async def _locked_release(
        self, session: AsyncSession, context: TenantContext, release_id: UUID
    ) -> ReleaseModel:
        model = await session.scalar(
            select(ReleaseModel)
            .where(
                ReleaseModel.tenant_id == UUID(context.tenant_id),
                ReleaseModel.id == release_id,
            )
            .with_for_update()
        )
        if model is None:
            raise resource_state_conflict("Release is unavailable.")
        return model

    @staticmethod
    async def _operation(
        session: AsyncSession, release: ReleaseModel
    ) -> OperationRecordModel:
        operation = await session.scalar(
            select(OperationRecordModel)
            .where(
                OperationRecordModel.tenant_id == release.tenant_id,
                OperationRecordModel.id == release.operation_id,
            )
            .with_for_update()
        )
        if operation is None:
            raise RuntimeError("Release Operation is unavailable")
        return operation


def _release_record(model: ReleaseModel) -> ReleaseRecord:
    return ReleaseRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        requested_by=model.requested_by,
        operation_id=model.operation_id,
        release_kind=cast(ReleaseKind, model.release_kind),
        expected_agent_version=model.expected_agent_version,
        requested_snapshot_id=model.requested_snapshot_id,
        runtime_targets=tuple(model.runtime_targets_json),
        release_note=model.release_note,
        run_smoke_test=model.run_smoke_test,
        activate_on_success=model.activate_on_success,
        status=cast(ReleaseStatus, model.status),
        workflow_id=model.workflow_id,
        snapshot_id=model.snapshot_id,
        deployment_ids=tuple(UUID(item) for item in model.deployment_ids_json),
        error_code=model.error_code,
        error_detail=cast(dict[str, JsonValue] | None, model.error_detail_json),
        created_at=model.created_at,
        started_at=model.started_at,
        finished_at=model.finished_at,
    )


def _runtime_bundle_record(model: RuntimeBundleModel) -> RuntimeBundleRecord:
    return RuntimeBundleRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        snapshot_id=model.snapshot_id,
        runtime_type=model.runtime_type,
        compiler_name=model.compiler_name,
        compiler_version=model.compiler_version,
        manifest_schema_version=model.manifest_schema_version,
        manifest=cast(dict[str, JsonValue], model.manifest_json),
        content_hash=model.content_hash,
        object_uri=model.object_uri,
        size_bytes=model.size_bytes,
        signature_ref=model.signature_ref,
        sbom_ref=model.sbom_ref,
        scan_status=cast(RuntimeBundleScanStatus, model.scan_status),
        created_at=model.created_at,
    )


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    release_id: UUID,
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    encoded = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type="release",
            resource_id=release_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_schema_version=1,
            metadata_json={"changed_fields": sorted(change)},
        )
    )
