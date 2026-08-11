"""PostgreSQL persistence for tenant-owned Artifact uploads and scans."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ARTIFACT_SCAN_REQUESTED_EVENT,
)
from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import ArtifactUploadCreateRequest
from packages.contracts.public import (
    TenantContext,
    dependency_unavailable,
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
    ArtifactModel,
    AuditLogModel,
    ChatSessionModel,
    OperationRecordModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyArtifactStore:
    """Persist Artifact state with owner scoping and atomic scan publication."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        except SQLAlchemyError as error:
            raise dependency_unavailable("Artifact Store is unavailable.") from error

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

                await session.flush()
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

    async def confirm_download(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
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
                await _audit(
                    unit.session,
                    context,
                    action="artifact.download.authorize",
                    artifact_id=artifact_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={"grant_expires_at": grant_expires_at.isoformat()},
                    occurred_at=max(now, model.updated_at),
                )
                return True
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
        deleted_at=model.deleted_at,
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
