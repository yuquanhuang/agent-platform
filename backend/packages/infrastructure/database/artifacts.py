"""PostgreSQL persistence for tenant-owned Artifact uploads and scans."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ARTIFACT_DOWNLOADS_REVOKED_EVENT,
    ARTIFACT_SCAN_REQUESTED_EVENT,
    ArtifactDownloadGrantRecord,
    ArtifactLegalHoldRecord,
    ArtifactRetentionPolicy,
)
from packages.application.metadata import RequestMetadata
from packages.application.policy import (
    ArtifactStorageAdmissionDenied,
    ArtifactStoragePolicy,
    TenantStoragePolicy,
    admit_artifact_storage,
)
from packages.contracts.generated.core_models import ArtifactUploadCreateRequest
from packages.contracts.public import (
    SubjectType,
    TenantContext,
    dependency_unavailable,
    rate_limited,
    resource_state_conflict,
)
from packages.domain.public import (
    ArtifactRecord,
    ArtifactStatus,
    MutationOutcome,
    OperationRecord,
    OperationStatus,
    OutboxEvent,
    OutboxStatus,
    artifact_uri,
    ensure_artifact_transition,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentRunModel,
    ArtifactDownloadGrantModel,
    ArtifactLegalHoldModel,
    ArtifactModel,
    AuditLogModel,
    ChatSessionModel,
    OperationRecordModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.storage_policies import (
    load_active_storage_policy_version,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork, TenantUnitOfWork


class SqlAlchemyArtifactStore:
    """Persist Artifact state with owner scoping and atomic scan publication."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage_policy: ArtifactStoragePolicy | None = None,
        retention_policy: ArtifactRetentionPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage_policy = storage_policy or ArtifactStoragePolicy()
        self._deployment_storage_policy = TenantStoragePolicy(
            max_reserved_artifact_bytes=(
                self._storage_policy.max_reserved_bytes_per_tenant
            ),
            max_reserved_artifacts=(
                self._storage_policy.max_reserved_artifacts_per_tenant
            ),
        )
        self._retention_policy = retention_policy or ArtifactRetentionPolicy()

    async def create_upload(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        request: ArtifactUploadCreateRequest,
        quarantine_object_uri: str,
        upload_expires_at: datetime,
        expires_at: datetime,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> ArtifactRecord:
        tenant_id = UUID(context.tenant_id)
        storage_policy_version_id: UUID | None = None
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=owner_user_id,
                    operation_type="artifact.upload.create",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    existing = await _owned_artifact(
                        session,
                        tenant_id=tenant_id,
                        artifact_id=artifact_id,
                        owner_user_id=owner_user_id,
                    )
                    if existing is None:
                        raise RuntimeError(
                            "Artifact idempotency replay target is unavailable"
                        )
                    return _artifact_record(existing)

                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": f"storage-policy:{tenant_id}"},
                )
                storage_policy_version_id, tenant_storage_policy = (
                    await load_active_storage_policy_version(session, tenant_id)
                )
                effective_storage_policy = self._deployment_storage_policy
                if tenant_storage_policy is not None:
                    effective_storage_policy = effective_storage_policy.narrowed_by(
                        tenant_storage_policy
                    )
                artifact_storage_policy = effective_storage_policy.artifact_policy()
                if artifact_storage_policy.enabled:
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:lock_key, 0))"
                        ),
                        {"lock_key": f"artifact-storage:{tenant_id}"},
                    )
                    reserved_bytes, reserved_artifacts = (
                        await session.execute(
                            select(
                                func.coalesce(func.sum(ArtifactModel.size_bytes), 0),
                                func.count(ArtifactModel.id),
                            ).where(
                                ArtifactModel.tenant_id == tenant_id,
                                ArtifactModel.status != "DELETED",
                            )
                        )
                    ).one()
                    admit_artifact_storage(
                        artifact_storage_policy,
                        reserved_bytes=int(reserved_bytes or 0),
                        reserved_artifacts=int(reserved_artifacts or 0),
                        requested_bytes=request.size,
                    )

                now = datetime.now(UTC)
                model = ArtifactModel(
                    id=artifact_id,
                    tenant_id=tenant_id,
                    workspace_id=None,
                    run_id=None,
                    owner_user_id=owner_user_id,
                    name=request.name,
                    quarantine_object_uri=quarantine_object_uri,
                    object_uri=None,
                    content_hash=request.content_hash,
                    size_bytes=request.size,
                    content_type=request.content_type,
                    status="UPLOADING",
                    required_output=False,
                    scan_result_json=None,
                    upload_expires_at=upload_expires_at,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    retention_delete_after=None,
                    deleted_at=None,
                )
                session.add(model)
                await session.flush()
                record = _artifact_record(model)
                await _audit(
                    session,
                    context,
                    action="artifact.upload.create",
                    artifact_id=artifact_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={
                        "content_type": request.content_type,
                        "size_bytes": request.size,
                        "storage_policy_version_id": (
                            str(storage_policy_version_id)
                            if storage_policy_version_id is not None
                            else None
                        ),
                    },
                    occurred_at=now,
                )
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=201,
                    response_body={"artifact_id": str(artifact_id)},
                    response_etag=None,
                    response_ref=str(artifact_id),
                )
                return record
        except ArtifactStorageAdmissionDenied as denial:
            await self._record_storage_denial(
                context,
                actor_id=owner_user_id,
                artifact_id=artifact_id,
                denial=denial,
                metadata=metadata,
                storage_policy_version_id=storage_policy_version_id,
            )
            raise rate_limited(
                details={
                    "scope": "tenant",
                    "reason_code": denial.reason_code,
                }
            ) from denial
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def _record_storage_denial(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        artifact_id: UUID,
        denial: ArtifactStorageAdmissionDenied,
        metadata: RequestMetadata,
        storage_policy_version_id: UUID | None,
    ) -> None:
        change = {
            "scope": "tenant",
            "dimension": denial.dimension,
            "reason_code": denial.reason_code,
            "current": denial.current,
            "requested": denial.requested,
            "limit": denial.limit,
            "storage_policy_version_id": (
                str(storage_policy_version_id)
                if storage_policy_version_id is not None
                else None
            ),
        }
        canonical = json.dumps(change, sort_keys=True, separators=(",", ":"))
        async with TenantUnitOfWork(self._session_factory, context) as unit:
            unit.session.add(
                AuditLogModel(
                    tenant_id=UUID(context.tenant_id),
                    actor_type="user",
                    actor_id=actor_id,
                    action="artifact.upload.admission_deny",
                    resource_type="artifact",
                    resource_id=artifact_id,
                    result="DENIED",
                    reason_codes=["RATE_LIMITED", denial.reason_code],
                    change_digest=(
                        "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
                    ),
                    request_id=metadata.request_id,
                    trace_id=metadata.trace_id,
                    metadata_json=change,
                )
            )

    async def get_owned(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
    ) -> ArtifactRecord | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                model = await _owned_artifact(
                    unit.session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                )
                return _artifact_record(model) if model is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def mark_scanning(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactRecord | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=owner_user_id,
                    operation_type="artifact.upload.complete",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                model = await _owned_artifact(
                    session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    lock=True,
                )
                if model is None:
                    return None
                if replay is not None:
                    return _artifact_record(model)

                current = cast(ArtifactStatus, model.status)
                if current == "UPLOADING":
                    effective_now = max(now, model.updated_at)
                    ensure_artifact_transition(current, "SCANNING")
                    model.status = "SCANNING"
                    model.updated_at = effective_now
                    SqlAlchemyOutboxWriter(session, context).add(
                        OutboxEvent(
                            id=uuid5(
                                NAMESPACE_URL,
                                f"artifact-scan-outbox/{tenant_id}/{artifact_id}",
                            ),
                            tenant_id=tenant_id,
                            aggregate_type="artifact",
                            aggregate_id=artifact_id,
                            event_type=ARTIFACT_SCAN_REQUESTED_EVENT,
                            payload={"artifact_id": str(artifact_id)},
                            payload_schema_version=1,
                            status=OutboxStatus.PENDING,
                            attempts=0,
                            next_attempt_at=effective_now,
                            created_at=effective_now,
                        )
                    )
                    await _audit(
                        session,
                        context,
                        action="artifact.upload.complete",
                        artifact_id=artifact_id,
                        result="SUCCESS",
                        reason_codes=[],
                        metadata={"status": "SCANNING"},
                        occurred_at=effective_now,
                    )
                elif current not in {"SCANNING", "AVAILABLE", "REJECTED", "FAILED"}:
                    raise ValueError("Artifact cannot complete from its current state")

                await session.flush()
                record = _artifact_record(model)
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=202,
                    response_body=_artifact_json(record),
                    response_etag=None,
                    response_ref=str(artifact_id),
                )
                return record
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def fail_upload(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                model = await _owned_artifact(
                    unit.session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    lock=True,
                )
                if model is None or model.status != "UPLOADING":
                    return
                effective_now = max(now, model.updated_at)
                ensure_artifact_transition("UPLOADING", "FAILED")
                model.status = "FAILED"
                model.scan_result_json = _failure_result(code, effective_now)
                model.retention_delete_after = (
                    effective_now + self._retention_policy.forensic_retention
                )
                model.updated_at = effective_now
                await _audit(
                    unit.session,
                    context,
                    action="artifact.upload.failed",
                    artifact_id=artifact_id,
                    result="FAILED",
                    reason_codes=[code],
                    metadata={"failure_code": code},
                    occurred_at=effective_now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def get_downloadable(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactRecord | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                model = await _owned_artifact(
                    unit.session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    lock=True,
                )
                if model is None:
                    return None
                if model.run_id is not None and not await _source_session_exists(
                    unit.session,
                    tenant_id=tenant_id,
                    run_id=model.run_id,
                    owner_user_id=owner_user_id,
                ):
                    return None
                if model.status == "AVAILABLE" and model.expires_at <= now:
                    effective_now = max(now, model.updated_at)
                    ensure_artifact_transition("AVAILABLE", "EXPIRED")
                    model.status = "EXPIRED"
                    model.retention_delete_after = (
                        effective_now + self._retention_policy.forensic_retention
                    )
                    model.updated_at = effective_now
                    await _audit(
                        unit.session,
                        context,
                        action="artifact.expire",
                        artifact_id=artifact_id,
                        result="SUCCESS",
                        reason_codes=["RETENTION_EXPIRED"],
                        metadata={"expires_at": model.expires_at.isoformat()},
                        occurred_at=effective_now,
                    )
                    await unit.session.flush()
                return _artifact_record(model)
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def request_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        now: datetime,
    ) -> MutationOutcome[OperationRecord] | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                model = await _owned_artifact(
                    session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    lock=True,
                )
                if model is None:
                    return None
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=owner_user_id,
                    operation_type="artifact.delete",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return MutationOutcome(replay=replay)

                existing_operation = await session.scalar(
                    select(OperationRecordModel)
                    .where(
                        OperationRecordModel.tenant_id == tenant_id,
                        OperationRecordModel.actor_id == owner_user_id,
                        OperationRecordModel.operation_type == "artifact.delete",
                        OperationRecordModel.resource_type == "artifact",
                        OperationRecordModel.resource_id == artifact_id,
                    )
                    .order_by(OperationRecordModel.created_at.desc())
                    .limit(1)
                )
                if model.status in {"UPLOADING", "SCANNING"}:
                    raise resource_state_conflict(
                        "Artifact upload or scanning must finish before deletion."
                    )
                if await _has_active_legal_hold(
                    session, tenant_id=tenant_id, artifact_id=artifact_id
                ):
                    raise resource_state_conflict(
                        "The Artifact is protected by an active legal hold."
                    )

                operation = existing_operation
                should_dispatch = False
                if model.status == "DELETED":
                    if operation is None or operation.status != "SUCCEEDED":
                        operation = _new_delete_operation(
                            tenant_id,
                            owner_user_id,
                            artifact_id,
                            status="SUCCEEDED",
                            now=now,
                        )
                        session.add(operation)
                elif (
                    model.status == "DELETING"
                    and operation is not None
                    and (operation.status in {"ACCEPTED", "RUNNING"})
                ):
                    pass
                else:
                    effective_now = max(now, model.updated_at)
                    if model.status != "DELETING":
                        ensure_artifact_transition(
                            cast(ArtifactStatus, model.status), "DELETING"
                        )
                        model.status = "DELETING"
                        model.updated_at = effective_now
                    operation = _new_delete_operation(
                        tenant_id,
                        owner_user_id,
                        artifact_id,
                        status="ACCEPTED",
                        now=effective_now,
                    )
                    session.add(operation)
                    should_dispatch = True

                revocation_time = max(now, model.updated_at)
                revoked_grant_ids = await _revoke_download_grants(
                    session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    now=revocation_time,
                )
                await session.flush()
                if revoked_grant_ids:
                    SqlAlchemyOutboxWriter(session, context).add(
                        OutboxEvent(
                            id=uuid5(
                                NAMESPACE_URL,
                                "artifact-download-revocation-outbox/"
                                f"{tenant_id}/{operation.id}",
                            ),
                            tenant_id=tenant_id,
                            aggregate_type="artifact",
                            aggregate_id=artifact_id,
                            event_type=ARTIFACT_DOWNLOADS_REVOKED_EVENT,
                            payload={
                                "tenant_id": str(tenant_id),
                                "artifact_id": str(artifact_id),
                                "revoked_at": revocation_time.isoformat(),
                                "grant_count": len(revoked_grant_ids),
                            },
                            payload_schema_version=1,
                            status=OutboxStatus.PENDING,
                            attempts=0,
                            next_attempt_at=revocation_time,
                            created_at=revocation_time,
                        )
                    )
                if should_dispatch:
                    SqlAlchemyOutboxWriter(session, context).add(
                        OutboxEvent(
                            id=uuid5(
                                NAMESPACE_URL,
                                f"artifact-delete-outbox/{tenant_id}/{operation.id}",
                            ),
                            tenant_id=tenant_id,
                            aggregate_type="artifact",
                            aggregate_id=artifact_id,
                            event_type=ARTIFACT_DELETE_REQUESTED_EVENT,
                            payload={
                                "artifact_id": str(artifact_id),
                                "operation_id": str(operation.id),
                            },
                            payload_schema_version=1,
                            status=OutboxStatus.PENDING,
                            attempts=0,
                            next_attempt_at=max(now, model.updated_at),
                            created_at=max(now, model.updated_at),
                        )
                    )
                    await _audit(
                        session,
                        context,
                        action="artifact.delete.request",
                        artifact_id=artifact_id,
                        result="SUCCESS",
                        reason_codes=[],
                        metadata={
                            "operation_id": str(operation.id),
                            "status": "DELETING",
                            "downloads_revoked": True,
                            "revoked_grant_count": len(revoked_grant_ids),
                        },
                        occurred_at=max(now, model.updated_at),
                    )
                response_body = _operation_accepted_json(operation.id)
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=202,
                    response_body=response_body,
                    response_etag=None,
                    response_ref=str(operation.id),
                )
                return MutationOutcome(value=_operation_record(operation))
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def create_download_grant(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        owner_user_id: UUID,
        token_hash: str,
        grant_expires_at: datetime,
        metadata: RequestMetadata,
        now: datetime,
    ) -> bool:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                model = await _owned_artifact(
                    unit.session,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    lock=True,
                )
                if (
                    model is None
                    or model.status != "AVAILABLE"
                    or model.expires_at <= now
                    or grant_expires_at > model.expires_at
                ):
                    return False
                if model.run_id is not None and not await _source_session_exists(
                    unit.session,
                    tenant_id=tenant_id,
                    run_id=model.run_id,
                    owner_user_id=owner_user_id,
                ):
                    return False
                unit.session.add(
                    ArtifactDownloadGrantModel(
                        id=grant_id,
                        tenant_id=tenant_id,
                        artifact_id=artifact_id,
                        owner_user_id=owner_user_id,
                        token_hash=token_hash,
                        expires_at=grant_expires_at,
                        revoked_at=None,
                        created_at=now,
                    )
                )
                await _audit(
                    unit.session,
                    context,
                    action="artifact.download.authorize",
                    artifact_id=artifact_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={
                        "grant_id": str(grant_id),
                        "grant_expires_at": grant_expires_at.isoformat(),
                    },
                    occurred_at=max(now, model.updated_at),
                )
                return True
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def resolve_download_grant(
        self,
        *,
        grant_id: UUID,
        token_hash: str,
        service_subject_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactDownloadGrantRecord | None:
        try:
            async with PlatformUnitOfWork(self._session_factory) as unit:
                grant = await unit.session.scalar(
                    select(ArtifactDownloadGrantModel).where(
                        ArtifactDownloadGrantModel.id == grant_id
                    )
                )
                if (
                    grant is None
                    or not hmac.compare_digest(grant.token_hash, token_hash)
                    or grant.revoked_at is not None
                    or grant.expires_at <= now
                ):
                    return None
                tenant_context = TenantContext(
                    tenant_id=str(grant.tenant_id),
                    subject_type=SubjectType.SERVICE,
                    subject_id=str(service_subject_id),
                    auth_time=now,
                    request_id=metadata.request_id,
                    trace_id=metadata.trace_id,
                )
            async with TenantUnitOfWork(
                self._session_factory, tenant_context, read_only=True
            ) as tenant_unit:
                artifact = await tenant_unit.session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.tenant_id == grant.tenant_id,
                        ArtifactModel.id == grant.artifact_id,
                        ArtifactModel.status == "AVAILABLE",
                        ArtifactModel.expires_at > now,
                    )
                )
                if artifact is None or artifact.owner_user_id != grant.owner_user_id:
                    return None
                return ArtifactDownloadGrantRecord(
                    id=grant.id,
                    tenant_id=grant.tenant_id,
                    artifact_id=grant.artifact_id,
                    owner_user_id=grant.owner_user_id,
                    token_hash=grant.token_hash,
                    expires_at=grant.expires_at,
                    revoked_at=grant.revoked_at,
                    artifact=_artifact_record(artifact),
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def record_download_open(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> bool:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                grant = await unit.session.scalar(
                    select(ArtifactDownloadGrantModel)
                    .where(
                        ArtifactDownloadGrantModel.tenant_id == tenant_id,
                        ArtifactDownloadGrantModel.id == grant_id,
                        ArtifactDownloadGrantModel.artifact_id == artifact_id,
                    )
                    .with_for_update()
                )
                if (
                    grant is None
                    or grant.revoked_at is not None
                    or grant.expires_at <= now
                ):
                    return False
                artifact = await unit.session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.tenant_id == tenant_id,
                        ArtifactModel.id == artifact_id,
                        ArtifactModel.status == "AVAILABLE",
                        ArtifactModel.expires_at > now,
                    )
                )
                if artifact is None:
                    return False
                await _audit(
                    unit.session,
                    context,
                    action="artifact.download.open",
                    artifact_id=artifact_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={"grant_id": str(grant_id)},
                    occurred_at=max(now, artifact.updated_at),
                )
                return True
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def is_download_grant_active(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> bool:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                grant = await unit.session.scalar(
                    select(ArtifactDownloadGrantModel).where(
                        ArtifactDownloadGrantModel.tenant_id == tenant_id,
                        ArtifactDownloadGrantModel.id == grant_id,
                        ArtifactDownloadGrantModel.artifact_id == artifact_id,
                        ArtifactDownloadGrantModel.revoked_at.is_(None),
                        ArtifactDownloadGrantModel.expires_at > now,
                    )
                )
                if grant is None:
                    return False
                artifact = await unit.session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.tenant_id == tenant_id,
                        ArtifactModel.id == artifact_id,
                        ArtifactModel.owner_user_id == grant.owner_user_id,
                        ArtifactModel.status == "AVAILABLE",
                        ArtifactModel.expires_at > now,
                    )
                )
                return artifact is not None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def get_for_scan(
        self, context: TenantContext, *, artifact_id: UUID
    ) -> ArtifactRecord | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                model = await unit.session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.tenant_id == tenant_id,
                        ArtifactModel.id == artifact_id,
                    )
                )
                return _artifact_record(model) if model is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def complete_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        status: Literal["AVAILABLE", "REJECTED"],
        object_uri: str | None,
        scan_result: dict[str, JsonValue],
        now: datetime,
    ) -> ArtifactRecord:
        tenant_id = UUID(context.tenant_id)
        expected_uri = artifact_uri(tenant_id=tenant_id, artifact_id=artifact_id)
        if status == "AVAILABLE" and object_uri != expected_uri:
            raise ValueError("Artifact trusted object URI is not canonical")
        if status == "REJECTED" and object_uri is not None:
            raise ValueError("Rejected Artifact cannot have a trusted object URI")
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                model = await _artifact_for_update(
                    unit.session, tenant_id=tenant_id, artifact_id=artifact_id
                )
                if model is None:
                    raise ValueError("Artifact scan target is unavailable")
                current = cast(ArtifactStatus, model.status)
                if current == status:
                    return _artifact_record(model)
                effective_now = max(now, model.updated_at)
                ensure_artifact_transition(current, status)
                model.status = status
                model.object_uri = object_uri
                model.scan_result_json = cast(dict[str, object], scan_result)
                model.retention_delete_after = (
                    effective_now + self._retention_policy.forensic_retention
                    if status == "REJECTED"
                    else None
                )
                model.updated_at = effective_now
                await _audit(
                    unit.session,
                    context,
                    action="artifact.scan.complete",
                    artifact_id=artifact_id,
                    result="SUCCESS" if status == "AVAILABLE" else "DENIED",
                    reason_codes=([] if status == "AVAILABLE" else ["SCAN_REJECTED"]),
                    metadata={"status": status},
                    occurred_at=effective_now,
                )
                await unit.session.flush()
                return _artifact_record(model)
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def fail_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                model = await _artifact_for_update(
                    unit.session, tenant_id=tenant_id, artifact_id=artifact_id
                )
                if model is None or model.status in {"AVAILABLE", "REJECTED", "FAILED"}:
                    return
                current = cast(ArtifactStatus, model.status)
                effective_now = max(now, model.updated_at)
                ensure_artifact_transition(current, "FAILED")
                model.status = "FAILED"
                model.object_uri = None
                model.scan_result_json = _failure_result(code, effective_now)
                model.retention_delete_after = (
                    effective_now + self._retention_policy.forensic_retention
                )
                model.updated_at = effective_now
                await _audit(
                    unit.session,
                    context,
                    action="artifact.scan.failed",
                    artifact_id=artifact_id,
                    result="FAILED",
                    reason_codes=[code],
                    metadata={"failure_code": code},
                    occurred_at=effective_now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def expire_due(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        if limit < 1:
            raise ValueError("Artifact expiration limit must be positive")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                models = list(
                    (
                        await unit.session.scalars(
                            select(ArtifactModel)
                            .where(
                                ArtifactModel.tenant_id == tenant_id,
                                ArtifactModel.status == "AVAILABLE",
                                ArtifactModel.expires_at <= now,
                            )
                            .order_by(ArtifactModel.expires_at, ArtifactModel.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for model in models:
                    effective_now = max(now, model.updated_at)
                    ensure_artifact_transition("AVAILABLE", "EXPIRED")
                    model.status = "EXPIRED"
                    model.retention_delete_after = (
                        effective_now + self._retention_policy.forensic_retention
                    )
                    model.updated_at = effective_now
                    await _audit(
                        unit.session,
                        context,
                        action="artifact.expire",
                        artifact_id=model.id,
                        result="SUCCESS",
                        reason_codes=["RETENTION_EXPIRED"],
                        metadata={"expires_at": model.expires_at.isoformat()},
                        occurred_at=effective_now,
                    )
                return len(models)
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def reclaim_expired_uploads(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """Atomically convert abandoned uploads into asynchronous deletion work."""

        if limit < 1:
            raise ValueError("Artifact upload reclamation limit must be positive")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                models = list(
                    (
                        await session.scalars(
                            select(ArtifactModel)
                            .where(
                                ArtifactModel.tenant_id == tenant_id,
                                ArtifactModel.status == "UPLOADING",
                                ArtifactModel.upload_expires_at <= now,
                            )
                            .order_by(ArtifactModel.upload_expires_at, ArtifactModel.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for model in models:
                    effective_now = max(now, model.updated_at)
                    ensure_artifact_transition("UPLOADING", "FAILED")
                    model.status = "FAILED"
                    model.scan_result_json = _failure_result(
                        "ARTIFACT_UPLOAD_EXPIRED", effective_now
                    )
                    model.retention_delete_after = effective_now
                    model.updated_at = effective_now
                    await _audit(
                        session,
                        context,
                        action="artifact.upload.expired",
                        artifact_id=model.id,
                        result="FAILED",
                        reason_codes=["ARTIFACT_UPLOAD_EXPIRED"],
                        metadata={
                            "failure_code": "ARTIFACT_UPLOAD_EXPIRED",
                            "upload_expires_at": model.upload_expires_at.isoformat(),
                            "automatic_reclamation": True,
                        },
                        occurred_at=effective_now,
                    )
                    # The database guard validates each persisted state transition.
                    await session.flush()

                    ensure_artifact_transition("FAILED", "DELETING")
                    model.status = "DELETING"
                    model.updated_at = effective_now
                    operation = _new_delete_operation(
                        tenant_id,
                        model.owner_user_id,
                        model.id,
                        status="ACCEPTED",
                        now=effective_now,
                    )
                    session.add(operation)
                    await session.flush()
                    SqlAlchemyOutboxWriter(session, context).add(
                        OutboxEvent(
                            id=uuid5(
                                NAMESPACE_URL,
                                f"artifact-delete-outbox/{tenant_id}/{operation.id}",
                            ),
                            tenant_id=tenant_id,
                            aggregate_type="artifact",
                            aggregate_id=model.id,
                            event_type=ARTIFACT_DELETE_REQUESTED_EVENT,
                            payload={
                                "artifact_id": str(model.id),
                                "operation_id": str(operation.id),
                            },
                            payload_schema_version=1,
                            status=OutboxStatus.PENDING,
                            attempts=0,
                            next_attempt_at=effective_now,
                            created_at=effective_now,
                        )
                    )
                    await _audit(
                        session,
                        context,
                        action="artifact.delete.request",
                        artifact_id=model.id,
                        result="SUCCESS",
                        reason_codes=["ARTIFACT_UPLOAD_EXPIRED"],
                        metadata={
                            "operation_id": str(operation.id),
                            "status": "DELETING",
                            "downloads_revoked": True,
                            "revoked_grant_count": 0,
                            "automatic_reclamation": True,
                            "trigger": "upload_window_expired",
                        },
                        occurred_at=effective_now,
                    )
                return len(models)
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def purge_retention_due(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """Claim forensic windows that elapsed and enqueue idempotent deletion."""

        if limit < 1:
            raise ValueError("Artifact retention purge limit must be positive")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                models = list(
                    (
                        await session.scalars(
                            select(ArtifactModel)
                            .where(
                                ArtifactModel.tenant_id == tenant_id,
                                ArtifactModel.status.in_(
                                    ("FAILED", "REJECTED", "EXPIRED")
                                ),
                                ArtifactModel.retention_delete_after.is_not(None),
                                ArtifactModel.retention_delete_after <= now,
                                ~select(ArtifactLegalHoldModel.id)
                                .where(
                                    ArtifactLegalHoldModel.tenant_id == tenant_id,
                                    ArtifactLegalHoldModel.artifact_id
                                    == ArtifactModel.id,
                                    ArtifactLegalHoldModel.released_at.is_(None),
                                )
                                .exists(),
                            )
                            .order_by(
                                ArtifactModel.retention_delete_after, ArtifactModel.id
                            )
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                purged = 0
                for model in models:
                    effective_now = max(now, model.updated_at)
                    await _enqueue_delete(
                        session,
                        context,
                        model=model,
                        actor_id=model.owner_user_id,
                        now=effective_now,
                        reason_codes=["RETENTION_DELETE_DUE"],
                        metadata={
                            "automatic_reclamation": True,
                            "trigger": "retention_delete_after",
                            "retention_delete_after": (
                                model.retention_delete_after.isoformat()
                                if model.retention_delete_after is not None
                                else None
                            ),
                        },
                    )
                    purged += 1
                return purged
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def recover_failed_deletes(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """Boundedly replace failed delete Operations after a cooldown."""

        if limit < 1:
            raise ValueError("Artifact delete recovery limit must be positive")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                models = list(
                    (
                        await unit.session.scalars(
                            select(ArtifactModel)
                            .where(
                                ArtifactModel.tenant_id == tenant_id,
                                ArtifactModel.status == "DELETING",
                            )
                            .order_by(ArtifactModel.updated_at, ArtifactModel.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                recovered = 0
                for model in models:
                    operation = await _latest_delete_operation(
                        unit.session, tenant_id=tenant_id, artifact_id=model.id
                    )
                    if (
                        operation is None
                        or operation.status != "FAILED"
                        or operation.finished_at is None
                        or operation.finished_at
                        + self._retention_policy.delete_recovery_delay
                        > now
                    ):
                        continue
                    operation_count = int(
                        await unit.session.scalar(
                            select(func.count(OperationRecordModel.id)).where(
                                OperationRecordModel.tenant_id == tenant_id,
                                OperationRecordModel.operation_type
                                == "artifact.delete",
                                OperationRecordModel.resource_type == "artifact",
                                OperationRecordModel.resource_id == model.id,
                            )
                        )
                        or 0
                    )
                    if (
                        operation_count
                        >= self._retention_policy.delete_recovery_max_operations
                    ):
                        continue
                    await _enqueue_delete(
                        unit.session,
                        context,
                        model=model,
                        actor_id=model.owner_user_id,
                        now=max(now, model.updated_at),
                        reason_codes=["ARTIFACT_DELETE_RECOVERY"],
                        metadata={
                            "automatic_recovery": True,
                            "previous_operation_id": str(operation.id),
                            "operation_attempt": operation_count + 1,
                        },
                        transition=False,
                    )
                    recovered += 1
                return recovered
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def get_for_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
    ) -> ArtifactRecord | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                operation_exists = await unit.session.scalar(
                    select(OperationRecordModel.id).where(
                        OperationRecordModel.tenant_id == tenant_id,
                        OperationRecordModel.id == operation_id,
                        OperationRecordModel.operation_type == "artifact.delete",
                        OperationRecordModel.resource_type == "artifact",
                        OperationRecordModel.resource_id == artifact_id,
                    )
                )
                if operation_exists is None:
                    return None
                model = await unit.session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.tenant_id == tenant_id,
                        ArtifactModel.id == artifact_id,
                    )
                )
                return _artifact_record(model) if model is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def complete_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        now: datetime,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                model = await _artifact_for_update(
                    session, tenant_id=tenant_id, artifact_id=artifact_id
                )
                operation = await session.scalar(
                    select(OperationRecordModel)
                    .where(
                        OperationRecordModel.tenant_id == tenant_id,
                        OperationRecordModel.id == operation_id,
                        OperationRecordModel.operation_type == "artifact.delete",
                        OperationRecordModel.resource_type == "artifact",
                        OperationRecordModel.resource_id == artifact_id,
                    )
                    .with_for_update()
                )
                if model is None or operation is None:
                    raise ValueError("Artifact delete target is unavailable")
                effective_now = max(now, model.updated_at, operation.updated_at)
                if model.status == "DELETING":
                    ensure_artifact_transition("DELETING", "DELETED")
                    model.status = "DELETED"
                    model.deleted_at = effective_now
                    model.updated_at = effective_now
                elif model.status != "DELETED":
                    raise ValueError("Artifact is not awaiting deletion")
                operation.status = "SUCCEEDED"
                operation.result_json = {
                    "artifact_id": str(artifact_id),
                    "status": "DELETED",
                }
                operation.error_json = None
                operation.updated_at = effective_now
                operation.finished_at = effective_now
                await _audit(
                    session,
                    context,
                    action="artifact.delete.complete",
                    artifact_id=artifact_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={
                        "operation_id": str(operation_id),
                        "status": "DELETED",
                    },
                    occurred_at=effective_now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def fail_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                operation = await unit.session.scalar(
                    select(OperationRecordModel)
                    .where(
                        OperationRecordModel.tenant_id == tenant_id,
                        OperationRecordModel.id == operation_id,
                        OperationRecordModel.operation_type == "artifact.delete",
                        OperationRecordModel.resource_type == "artifact",
                        OperationRecordModel.resource_id == artifact_id,
                    )
                    .with_for_update()
                )
                if operation is None or operation.status == "SUCCEEDED":
                    return
                effective_now = max(now, operation.updated_at)
                operation.status = "FAILED"
                operation.error_json = {
                    "code": code,
                    "message": "Artifact object cleanup did not complete.",
                }
                operation.updated_at = effective_now
                operation.finished_at = effective_now
                await _audit(
                    unit.session,
                    context,
                    action="artifact.delete.failed",
                    artifact_id=artifact_id,
                    result="FAILED",
                    reason_codes=[code],
                    metadata={"operation_id": str(operation_id)},
                    occurred_at=effective_now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

    async def place_legal_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord:
        tenant_id = UUID(context.tenant_id)
        actor_id = UUID(context.subject_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit:
            model = await _artifact_for_update(
                unit.session, tenant_id=tenant_id, artifact_id=artifact_id
            )
            if model is None:
                raise ValueError("Artifact legal-hold target is unavailable")
            if model.status in {"DELETING", "DELETED"}:
                raise ValueError("Artifact deletion has already started")
            existing = await unit.session.scalar(
                select(ArtifactLegalHoldModel).where(
                    ArtifactLegalHoldModel.tenant_id == tenant_id,
                    ArtifactLegalHoldModel.artifact_id == artifact_id,
                    ArtifactLegalHoldModel.case_ref == case_ref,
                    ArtifactLegalHoldModel.released_at.is_(None),
                )
            )
            if existing is not None:
                return _legal_hold_record(existing)
            hold = ArtifactLegalHoldModel(
                id=uuid4(),
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                case_ref=case_ref,
                reason=reason,
                placed_by=actor_id,
                placed_at=now,
                released_by=None,
                released_at=None,
            )
            unit.session.add(hold)
            await _audit(
                unit.session,
                context,
                action="artifact.legal_hold.place",
                artifact_id=artifact_id,
                result="SUCCESS",
                reason_codes=["LEGAL_HOLD"],
                metadata={"case_ref": case_ref, "reason": reason},
                occurred_at=now,
            )
            await unit.session.flush()
            return _legal_hold_record(hold)

    async def release_legal_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord | None:
        tenant_id = UUID(context.tenant_id)
        actor_id = UUID(context.subject_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit:
            model = await _artifact_for_update(
                unit.session, tenant_id=tenant_id, artifact_id=artifact_id
            )
            if model is None:
                return None
            hold = await unit.session.scalar(
                select(ArtifactLegalHoldModel)
                .where(
                    ArtifactLegalHoldModel.tenant_id == tenant_id,
                    ArtifactLegalHoldModel.artifact_id == artifact_id,
                    ArtifactLegalHoldModel.case_ref == case_ref,
                    ArtifactLegalHoldModel.released_at.is_(None),
                )
                .with_for_update()
            )
            if hold is None:
                return None
            hold.released_by = actor_id
            hold.released_at = now
            await _audit(
                unit.session,
                context,
                action="artifact.legal_hold.release",
                artifact_id=artifact_id,
                result="SUCCESS",
                reason_codes=["LEGAL_HOLD_RELEASED"],
                metadata={"case_ref": case_ref, "reason": reason},
                occurred_at=now,
            )
            await unit.session.flush()
            return _legal_hold_record(hold)

    async def retry_failed_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        reason: str,
        now: datetime,
    ) -> UUID | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit:
            model = await _artifact_for_update(
                unit.session, tenant_id=tenant_id, artifact_id=artifact_id
            )
            if model is None or model.status != "DELETING":
                return None
            latest = await _latest_delete_operation(
                unit.session, tenant_id=tenant_id, artifact_id=artifact_id
            )
            if latest is None or latest.status != "FAILED":
                return None
            operation_count = int(
                await unit.session.scalar(
                    select(func.count(OperationRecordModel.id)).where(
                        OperationRecordModel.tenant_id == tenant_id,
                        OperationRecordModel.operation_type == "artifact.delete",
                        OperationRecordModel.resource_type == "artifact",
                        OperationRecordModel.resource_id == artifact_id,
                    )
                )
                or 0
            )
            if operation_count >= self._retention_policy.delete_recovery_max_operations:
                return None
            operation = await _enqueue_delete(
                unit.session,
                context,
                model=model,
                actor_id=UUID(context.subject_id),
                now=max(now, model.updated_at),
                reason_codes=["ARTIFACT_DELETE_MANUAL_RETRY"],
                metadata={"reason": reason, "previous_operation_id": str(latest.id)},
                transition=False,
            )
            return operation.id


async def _revoke_download_grants(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    artifact_id: UUID,
    now: datetime,
) -> tuple[UUID, ...]:
    grants = (
        await session.scalars(
            select(ArtifactDownloadGrantModel)
            .where(
                ArtifactDownloadGrantModel.tenant_id == tenant_id,
                ArtifactDownloadGrantModel.artifact_id == artifact_id,
                ArtifactDownloadGrantModel.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    for grant in grants:
        grant.revoked_at = max(now, grant.created_at)
    return tuple(grant.id for grant in grants)


async def _has_active_legal_hold(
    session: AsyncSession, *, tenant_id: UUID, artifact_id: UUID
) -> bool:
    return (
        await session.scalar(
            select(ArtifactLegalHoldModel.id).where(
                ArtifactLegalHoldModel.tenant_id == tenant_id,
                ArtifactLegalHoldModel.artifact_id == artifact_id,
                ArtifactLegalHoldModel.released_at.is_(None),
            )
        )
        is not None
    )


async def _latest_delete_operation(
    session: AsyncSession, *, tenant_id: UUID, artifact_id: UUID
) -> OperationRecordModel | None:
    return await session.scalar(
        select(OperationRecordModel)
        .where(
            OperationRecordModel.tenant_id == tenant_id,
            OperationRecordModel.operation_type == "artifact.delete",
            OperationRecordModel.resource_type == "artifact",
            OperationRecordModel.resource_id == artifact_id,
        )
        .order_by(
            OperationRecordModel.created_at.desc(), OperationRecordModel.id.desc()
        )
        .limit(1)
    )


async def _enqueue_delete(
    session: AsyncSession,
    context: TenantContext,
    *,
    model: ArtifactModel,
    actor_id: UUID,
    now: datetime,
    reason_codes: list[str],
    metadata: dict[str, object],
    transition: bool = True,
) -> OperationRecordModel:
    if transition:
        ensure_artifact_transition(cast(ArtifactStatus, model.status), "DELETING")
        model.status = "DELETING"
        model.updated_at = now
    operation = _new_delete_operation(
        model.tenant_id, actor_id, model.id, status="ACCEPTED", now=now
    )
    session.add(operation)
    await session.flush()
    SqlAlchemyOutboxWriter(session, context).add(
        OutboxEvent(
            id=uuid5(
                NAMESPACE_URL,
                f"artifact-delete-outbox/{model.tenant_id}/{operation.id}",
            ),
            tenant_id=model.tenant_id,
            aggregate_type="artifact",
            aggregate_id=model.id,
            event_type=ARTIFACT_DELETE_REQUESTED_EVENT,
            payload={"artifact_id": str(model.id), "operation_id": str(operation.id)},
            payload_schema_version=1,
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )
    await _audit(
        session,
        context,
        action="artifact.delete.request",
        artifact_id=model.id,
        result="SUCCESS",
        reason_codes=reason_codes,
        metadata={"operation_id": str(operation.id), "status": "DELETING", **metadata},
        occurred_at=now,
    )
    return operation


async def _owned_artifact(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    artifact_id: UUID,
    owner_user_id: UUID,
    lock: bool = False,
) -> ArtifactModel | None:
    statement = select(ArtifactModel).where(
        ArtifactModel.tenant_id == tenant_id,
        ArtifactModel.id == artifact_id,
        ArtifactModel.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _artifact_for_update(
    session: AsyncSession, *, tenant_id: UUID, artifact_id: UUID
) -> ArtifactModel | None:
    return await session.scalar(
        select(ArtifactModel)
        .where(
            ArtifactModel.tenant_id == tenant_id,
            ArtifactModel.id == artifact_id,
        )
        .with_for_update()
    )


async def _source_session_exists(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    run_id: UUID,
    owner_user_id: UUID,
) -> bool:
    source_session = await session.scalar(
        select(ChatSessionModel.id)
        .join(
            AgentRunModel,
            and_(
                AgentRunModel.tenant_id == ChatSessionModel.tenant_id,
                AgentRunModel.session_id == ChatSessionModel.id,
            ),
        )
        .where(
            AgentRunModel.tenant_id == tenant_id,
            AgentRunModel.id == run_id,
            ChatSessionModel.user_id == owner_user_id,
            ChatSessionModel.status != "DELETED",
        )
    )
    return source_session is not None


def _artifact_record(model: ArtifactModel) -> ArtifactRecord:
    return ArtifactRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        workspace_id=model.workspace_id,
        run_id=model.run_id,
        owner_user_id=model.owner_user_id,
        name=model.name,
        quarantine_object_uri=model.quarantine_object_uri,
        object_uri=model.object_uri,
        content_hash=model.content_hash,
        size_bytes=model.size_bytes,
        content_type=model.content_type,
        status=cast(ArtifactStatus, model.status),
        required_output=model.required_output,
        scan_result=cast(dict[str, JsonValue] | None, model.scan_result_json),
        upload_expires_at=model.upload_expires_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        expires_at=model.expires_at,
        retention_delete_after=model.retention_delete_after,
        deleted_at=model.deleted_at,
    )


def _legal_hold_record(model: ArtifactLegalHoldModel) -> ArtifactLegalHoldRecord:
    return ArtifactLegalHoldRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        artifact_id=model.artifact_id,
        case_ref=model.case_ref,
        reason=model.reason,
        placed_by=model.placed_by,
        placed_at=model.placed_at,
        released_by=model.released_by,
        released_at=model.released_at,
    )


def _artifact_json(record: ArtifactRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "name": record.name,
        "status": record.status,
        "size": record.size_bytes,
        "content_type": record.content_type,
        "hash": record.content_hash,
        "run_id": str(record.run_id) if record.run_id is not None else None,
        "expires_at": record.expires_at.isoformat(),
        "created_at": record.created_at.isoformat(),
    }


def _new_delete_operation(
    tenant_id: UUID,
    actor_id: UUID,
    artifact_id: UUID,
    *,
    status: Literal["ACCEPTED", "SUCCEEDED"],
    now: datetime,
) -> OperationRecordModel:
    terminal = status == "SUCCEEDED"
    return OperationRecordModel(
        tenant_id=tenant_id,
        actor_id=actor_id,
        operation_type="artifact.delete",
        status=status,
        resource_type="artifact",
        resource_id=artifact_id,
        result_json=(
            {"artifact_id": str(artifact_id), "status": "DELETED"} if terminal else None
        ),
        error_json=None,
        created_at=now,
        updated_at=now,
        finished_at=now if terminal else None,
    )


def _operation_record(model: OperationRecordModel) -> OperationRecord:
    return OperationRecord(
        id=model.id,
        operation_type=model.operation_type,
        status=cast(OperationStatus, model.status),
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        result=cast(dict[str, JsonValue] | None, model.result_json),
        error=cast(dict[str, JsonValue] | None, model.error_json),
        created_at=model.created_at,
        updated_at=model.updated_at,
        finished_at=model.finished_at,
    )


def _operation_accepted_json(operation_id: UUID) -> dict[str, object]:
    return {
        "operation_id": str(operation_id),
        "status": "ACCEPTED",
        "status_url": f"/api/v1/operations/{operation_id}",
    }


def _failure_result(code: str, occurred_at: datetime) -> dict[str, object]:
    return {
        "schema_version": "artifact-failure/v1",
        "decision": "FAILED",
        "failure_code": code,
        "occurred_at": occurred_at.isoformat(),
    }


async def _audit(
    session: AsyncSession,
    context: TenantContext,
    *,
    action: str,
    artifact_id: UUID,
    result: Literal["SUCCESS", "FAILED", "DENIED"],
    reason_codes: list[str],
    metadata: dict[str, object],
    occurred_at: datetime,
) -> None:
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            id=uuid4(),
            tenant_id=UUID(context.tenant_id),
            actor_type=context.subject_type.value,
            actor_id=UUID(context.subject_id),
            action=action,
            resource_type="artifact",
            resource_id=artifact_id,
            result=result,
            reason_codes=reason_codes,
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_schema_version=1,
            metadata_json=metadata,
            created_at=occurred_at,
        )
    )
