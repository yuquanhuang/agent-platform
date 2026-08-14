"""PostgreSQL persistence for durable tenant storage capacity policies."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.metadata import RequestMetadata
from packages.application.policy import StoragePolicyStore, TenantStoragePolicy
from packages.contracts.generated.resources_models import (
    ActionRequest,
    StoragePolicyCreateRequest,
    StoragePolicyUpdateRequest,
)
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    MutationOutcome,
    StorageLimitsRecord,
    StoragePolicyRecord,
    StoragePolicyStatus,
    StoragePolicyVersionRecord,
    decode_cursor,
    encode_cursor,
    format_etag,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency as _claim_idempotency,
)
from packages.infrastructure.database.idempotency import (
    complete_idempotency as _complete_idempotency,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    StoragePolicyModel,
    StoragePolicyVersionModel,
    TenantModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyStoragePolicyStore(StoragePolicyStore):
    """Own tenant transactions for one active immutable storage policy version."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_policies(
        self, context: TenantContext, *, limit: int, cursor: str | None
    ) -> tuple[list[StoragePolicyRecord], str | None]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            statement = (
                select(StoragePolicyModel, StoragePolicyVersionModel)
                .join(
                    StoragePolicyVersionModel,
                    and_(
                        StoragePolicyVersionModel.tenant_id
                        == StoragePolicyModel.tenant_id,
                        StoragePolicyVersionModel.policy_id == StoragePolicyModel.id,
                        StoragePolicyVersionModel.id
                        == StoragePolicyModel.current_version_id,
                    ),
                )
                .where(StoragePolicyModel.tenant_id == tenant_id)
                .order_by(
                    StoragePolicyModel.created_at.desc(), StoragePolicyModel.id.desc()
                )
            )
            if cursor is not None:
                created_at, policy_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        StoragePolicyModel.created_at < created_at,
                        and_(
                            StoragePolicyModel.created_at == created_at,
                            StoragePolicyModel.id < policy_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.execute(statement.limit(limit + 1))).all()
            )
            page_rows = rows[:limit]
            next_cursor = (
                encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
                if len(rows) > limit and page_rows
                else None
            )
            return [
                _policy_record(policy, version) for policy, version in page_rows
            ], next_cursor

    async def create_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: StoragePolicyCreateRequest,
        limits: TenantStoragePolicy,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[StoragePolicyRecord]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            await _lock_storage_policy(session, tenant_id)
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="storage_policy.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            existing = await session.scalar(
                select(StoragePolicyModel.id).where(
                    StoragePolicyModel.tenant_id == tenant_id
                )
            )
            if existing is not None:
                raise resource_state_conflict(
                    "The tenant already has a StoragePolicy; update its immutable version."
                )
            policy_id = uuid4()
            version_id = uuid4()
            now = datetime.now(UTC)
            version = _new_version(
                version_id=version_id,
                tenant_id=tenant_id,
                policy_id=policy_id,
                version_no=1,
                limits=limits,
                actor_id=actor_id,
                now=now,
            )
            policy = StoragePolicyModel(
                id=policy_id,
                tenant_id=tenant_id,
                name=request.name,
                description=request.description,
                status="ACTIVE",
                current_version_id=version_id,
                resource_version=1,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add_all((policy, version))
            tenant = await session.scalar(
                select(TenantModel).where(TenantModel.id == tenant_id).with_for_update()
            )
            if tenant is None:
                raise resource_state_conflict("The active tenant is unavailable.")
            tenant.storage_policy_id = policy_id
            tenant.resource_version += 1
            tenant.updated_at = now
            await session.flush()
            result = _policy_record(policy, version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.create",
                resource_id=policy_id,
                metadata=metadata,
                change={
                    "name": request.name,
                    "status": "ACTIVE",
                    "version_no": 1,
                    "content_hash": version.content_hash,
                    "limits": _limits_json(limits),
                },
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_policy_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_policy(
        self, context: TenantContext, policy_id: UUID
    ) -> StoragePolicyRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            row = (
                await unit_of_work.session.execute(
                    _policy_statement(tenant_id, policy_id)
                )
            ).one_or_none()
            return _policy_record(*row) if row is not None else None

    async def update_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        request: StoragePolicyUpdateRequest,
        limits: TenantStoragePolicy | None,
        metadata: RequestMetadata,
    ) -> StoragePolicyRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            await _lock_storage_policy(session, tenant_id)
            policy = await session.scalar(
                select(StoragePolicyModel)
                .where(
                    StoragePolicyModel.tenant_id == tenant_id,
                    StoragePolicyModel.id == policy_id,
                )
                .with_for_update()
            )
            if policy is None:
                return None
            _check_version(policy.resource_version, expected_version)
            current = await session.scalar(
                select(StoragePolicyVersionModel).where(
                    StoragePolicyVersionModel.tenant_id == tenant_id,
                    StoragePolicyVersionModel.policy_id == policy_id,
                    StoragePolicyVersionModel.id == policy.current_version_id,
                )
            )
            if current is None:
                raise RuntimeError("StoragePolicy current version is unavailable")
            if request.name is not None:
                policy.name = request.name
            if "description" in request.model_fields_set:
                policy.description = request.description
            next_version = current
            if limits is not None:
                next_version = _new_version(
                    version_id=uuid4(),
                    tenant_id=tenant_id,
                    policy_id=policy_id,
                    version_no=current.version_no + 1,
                    limits=limits,
                    actor_id=actor_id,
                    now=datetime.now(UTC),
                )
                session.add(next_version)
                policy.current_version_id = next_version.id
            policy.resource_version += 1
            policy.updated_at = datetime.now(UTC)
            await session.flush()
            result = _policy_record(policy, next_version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.update",
                resource_id=policy_id,
                metadata=metadata,
                change={
                    "fields": sorted(request.model_fields_set),
                    "version_no": next_version.version_no,
                    "content_hash": next_version.content_hash,
                },
            )
            return result

    async def set_policy_status(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[StoragePolicyRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            await _lock_storage_policy(session, tenant_id)
            operation = "enable" if enabled else "disable"
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"storage_policy.{operation}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            policy = await session.scalar(
                select(StoragePolicyModel)
                .where(
                    StoragePolicyModel.tenant_id == tenant_id,
                    StoragePolicyModel.id == policy_id,
                )
                .with_for_update()
            )
            if policy is None:
                return None
            _check_version(policy.resource_version, expected_version)
            target_status = "ACTIVE" if enabled else "DISABLED"
            if policy.status == target_status:
                raise resource_state_conflict(
                    f"StoragePolicy is already {target_status.lower()}."
                )
            version = await session.scalar(
                select(StoragePolicyVersionModel).where(
                    StoragePolicyVersionModel.tenant_id == tenant_id,
                    StoragePolicyVersionModel.policy_id == policy_id,
                    StoragePolicyVersionModel.id == policy.current_version_id,
                )
            )
            if version is None:
                raise RuntimeError("StoragePolicy current version is unavailable")
            policy.status = target_status
            policy.resource_version += 1
            policy.updated_at = datetime.now(UTC)
            await session.flush()
            result = _policy_record(policy, version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=f"resource.{operation}",
                resource_id=policy_id,
                metadata=metadata,
                change={
                    "status": target_status,
                    "reason_present": bool(request and request.reason),
                },
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=200,
                response_body=_policy_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def list_versions(
        self,
        context: TenantContext,
        *,
        policy_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[StoragePolicyVersionRecord], str | None] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            exists = await session.scalar(
                select(StoragePolicyModel.id).where(
                    StoragePolicyModel.tenant_id == tenant_id,
                    StoragePolicyModel.id == policy_id,
                )
            )
            if exists is None:
                return None
            statement = (
                select(StoragePolicyVersionModel)
                .where(
                    StoragePolicyVersionModel.tenant_id == tenant_id,
                    StoragePolicyVersionModel.policy_id == policy_id,
                )
                .order_by(
                    StoragePolicyVersionModel.created_at.desc(),
                    StoragePolicyVersionModel.id.desc(),
                )
            )
            if cursor is not None:
                created_at, version_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        StoragePolicyVersionModel.created_at < created_at,
                        and_(
                            StoragePolicyVersionModel.created_at == created_at,
                            StoragePolicyVersionModel.id < version_id,
                        ),
                    )
                )
            rows = list(await session.scalars(statement.limit(limit + 1)))
            page_rows = rows[:limit]
            next_cursor = (
                encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if len(rows) > limit and page_rows
                else None
            )
            return [_version_record(row) for row in page_rows], next_cursor


async def load_active_storage_policy(
    session: AsyncSession, tenant_id: UUID
) -> TenantStoragePolicy | None:
    """Load the tenant's selected ACTIVE policy inside the Run transaction."""

    row = (
        await session.execute(
            select(StoragePolicyVersionModel)
            .join(
                StoragePolicyModel,
                and_(
                    StoragePolicyModel.tenant_id == StoragePolicyVersionModel.tenant_id,
                    StoragePolicyModel.id == StoragePolicyVersionModel.policy_id,
                    StoragePolicyModel.current_version_id
                    == StoragePolicyVersionModel.id,
                ),
            )
            .join(
                TenantModel,
                and_(
                    TenantModel.id == StoragePolicyModel.tenant_id,
                    TenantModel.storage_policy_id == StoragePolicyModel.id,
                ),
            )
            .where(
                StoragePolicyModel.tenant_id == tenant_id,
                StoragePolicyModel.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    return _storage_policy(row) if row is not None else None


async def load_active_storage_policy_version(
    session: AsyncSession, tenant_id: UUID
) -> tuple[UUID | None, TenantStoragePolicy | None]:
    """Load the immutable version identity and capacity limits in one query."""

    row = (
        await session.execute(
            select(StoragePolicyVersionModel)
            .join(
                StoragePolicyModel,
                and_(
                    StoragePolicyModel.tenant_id == StoragePolicyVersionModel.tenant_id,
                    StoragePolicyModel.id == StoragePolicyVersionModel.policy_id,
                    StoragePolicyModel.current_version_id
                    == StoragePolicyVersionModel.id,
                ),
            )
            .join(
                TenantModel,
                and_(
                    TenantModel.id == StoragePolicyModel.tenant_id,
                    TenantModel.storage_policy_id == StoragePolicyModel.id,
                ),
            )
            .where(
                StoragePolicyModel.tenant_id == tenant_id,
                StoragePolicyModel.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None, None
    return row.id, _storage_policy(row)


async def _lock_storage_policy(session: AsyncSession, tenant_id: UUID) -> None:
    """Serialize policy mutations with Artifact and Workspace admission."""

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"storage-policy:{tenant_id}"},
    )


def _policy_statement(tenant_id: UUID, policy_id: UUID):  # type: ignore[no-untyped-def]
    return (
        select(StoragePolicyModel, StoragePolicyVersionModel)
        .join(
            StoragePolicyVersionModel,
            and_(
                StoragePolicyVersionModel.tenant_id == StoragePolicyModel.tenant_id,
                StoragePolicyVersionModel.policy_id == StoragePolicyModel.id,
                StoragePolicyVersionModel.id == StoragePolicyModel.current_version_id,
            ),
        )
        .where(
            StoragePolicyModel.tenant_id == tenant_id,
            StoragePolicyModel.id == policy_id,
        )
    )


def _new_version(
    *,
    version_id: UUID,
    tenant_id: UUID,
    policy_id: UUID,
    version_no: int,
    limits: TenantStoragePolicy,
    actor_id: UUID,
    now: datetime,
) -> StoragePolicyVersionModel:
    values = _limits_json(limits)
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return StoragePolicyVersionModel(
        id=version_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        version_no=version_no,
        **values,
        content_hash=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        created_by=actor_id,
        created_at=now,
    )


def _storage_policy(model: StoragePolicyVersionModel) -> TenantStoragePolicy:
    return TenantStoragePolicy(
        max_reserved_workspace_bytes=model.max_reserved_workspace_bytes,
        max_reserved_workspaces=model.max_reserved_workspaces,
        max_reserved_artifact_bytes=model.max_reserved_artifact_bytes,
        max_reserved_artifacts=model.max_reserved_artifacts,
    )


def _policy_record(
    policy: StoragePolicyModel, version: StoragePolicyVersionModel
) -> StoragePolicyRecord:
    return StoragePolicyRecord(
        id=policy.id,
        tenant_id=policy.tenant_id,
        name=policy.name,
        description=policy.description,
        status=cast(StoragePolicyStatus, policy.status),
        current_version=_version_record(version),
        resource_version=policy.resource_version,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


def _version_record(model: StoragePolicyVersionModel) -> StoragePolicyVersionRecord:
    return StoragePolicyVersionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        policy_id=model.policy_id,
        version_no=model.version_no,
        limits=StorageLimitsRecord(
            max_reserved_workspace_bytes=model.max_reserved_workspace_bytes,
            max_reserved_workspaces=model.max_reserved_workspaces,
            max_reserved_artifact_bytes=model.max_reserved_artifact_bytes,
            max_reserved_artifacts=model.max_reserved_artifacts,
        ),
        content_hash=model.content_hash,
        created_by=model.created_by,
        created_at=model.created_at,
    )


def _limits_json(policy: TenantStoragePolicy) -> dict[str, int]:
    return {
        field_name: value
        for field_name in (
            "max_reserved_workspace_bytes",
            "max_reserved_workspaces",
            "max_reserved_artifact_bytes",
            "max_reserved_artifacts",
        )
        if (value := getattr(policy, field_name)) is not None
    }


def _policy_json(record: StoragePolicyRecord) -> dict[str, object]:
    version = record.current_version
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "current_version": {
            "id": str(version.id),
            "policy_id": str(version.policy_id),
            "version_no": version.version_no,
            "limits": {
                field_name: value
                for field_name in (
                    "max_reserved_workspace_bytes",
                    "max_reserved_workspaces",
                    "max_reserved_artifact_bytes",
                    "max_reserved_artifacts",
                )
                if (value := getattr(version.limits, field_name)) is not None
            },
            "content_hash": version.content_hash,
            "created_by": str(version.created_by),
            "created_at": version.created_at.isoformat(),
        },
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _check_version(current: int, expected: int) -> None:
    if current != expected:
        raise resource_version_conflict()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise validation_error("Pagination cursor is invalid.") from exc


async def _add_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: UUID,
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type="storage_policy",
            resource_id=resource_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )
