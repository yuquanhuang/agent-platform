"""PostgreSQL Workspace identity, quota accounting, and lifecycle persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.sandbox.policy import FrozenSandboxPolicy
from packages.contracts.public import PlatformError, TenantContext
from packages.domain.public import (
    WorkspaceRecord,
    WorkspaceStatus,
    WorkspaceUri,
    ensure_workspace_transition,
    ensure_workspace_usage,
)
from packages.infrastructure.database.models import WorkspaceModel
from packages.infrastructure.database.uow import TenantUnitOfWork

_MEBIBYTE = 1024 * 1024
_RUN_WORKSPACE_RETENTION = timedelta(days=7)


class SqlAlchemyWorkspaceStore:
    """Expose bounded usage updates without revealing a physical path."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, context: TenantContext, *, workspace_uri: str
    ) -> WorkspaceRecord | None:
        parsed = _parse_workspace_uri(workspace_uri)
        if parsed.tenant_id != context.tenant_id:
            return None
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit:
            workspace = await unit.session.scalar(
                select(WorkspaceModel).where(
                    WorkspaceModel.tenant_id == UUID(context.tenant_id),
                    WorkspaceModel.uri == parsed.to_string(),
                )
            )
            return _workspace_record(workspace) if workspace is not None else None

    async def record_usage(
        self,
        context: TenantContext,
        *,
        workspace_uri: str,
        used_bytes: int,
        file_count: int,
        now: datetime,
    ) -> WorkspaceRecord | None:
        parsed = _parse_workspace_uri(workspace_uri)
        if parsed.tenant_id != context.tenant_id:
            return None
        async with TenantUnitOfWork(self._session_factory, context) as unit:
            workspace = await unit.session.scalar(
                select(WorkspaceModel)
                .where(
                    WorkspaceModel.tenant_id == UUID(context.tenant_id),
                    WorkspaceModel.uri == parsed.to_string(),
                )
                .with_for_update()
            )
            if workspace is None:
                return None
            if workspace.status not in {"ACTIVE", "SEALED"}:
                raise _workspace_error(
                    409,
                    "WORKSPACE_URI_INVALID",
                    "Workspace usage cannot be updated in its current state.",
                )
            try:
                ensure_workspace_usage(
                    quota_bytes=workspace.quota_bytes,
                    used_bytes=used_bytes,
                    max_files=workspace.max_files,
                    file_count=file_count,
                )
            except ValueError as error:
                raise _workspace_error(
                    409,
                    "WORKSPACE_QUOTA_EXCEEDED",
                    "Workspace usage exceeds its frozen quota.",
                ) from error
            workspace.used_bytes = used_bytes
            workspace.file_count = file_count
            workspace.updated_at = now
            await unit.session.flush()
            return _workspace_record(workspace)


async def ensure_workspace_for_sandbox(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
    run_id: UUID,
    workspace_uri: str,
    policy: FrozenSandboxPolicy,
    now: datetime,
) -> WorkspaceModel:
    parsed = _parse_workspace_uri(workspace_uri)
    expected = WorkspaceUri.root(
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        session_id=str(session_id),
        run_id=str(run_id),
    )
    if parsed != expected:
        raise _workspace_error(
            400,
            "WORKSPACE_URI_INVALID",
            "Workspace identity does not match the Sandbox Run.",
        )
    effective = policy.load()
    quota_bytes = effective.disk_mb * _MEBIBYTE
    existing = await session.scalar(
        select(WorkspaceModel)
        .where(
            WorkspaceModel.tenant_id == tenant_id,
            WorkspaceModel.run_id == run_id,
        )
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.user_id != user_id
            or existing.session_id != session_id
            or existing.uri != parsed.to_string()
            or existing.quota_bytes != quota_bytes
            or existing.max_files != effective.filesystem.max_files
            or existing.max_file_bytes != effective.filesystem.max_file_bytes
            or existing.status != "ACTIVE"
        ):
            raise _workspace_error(
                409,
                "WORKSPACE_URI_INVALID",
                "The Run Workspace identity or frozen quota has changed.",
            )
        return existing
    workspace = WorkspaceModel(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        uri=parsed.to_string(),
        quota_bytes=quota_bytes,
        used_bytes=0,
        max_files=effective.filesystem.max_files,
        file_count=0,
        max_file_bytes=effective.filesystem.max_file_bytes,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
        expires_at=now + _RUN_WORKSPACE_RETENTION,
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def transition_workspace_for_sandbox(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    workspace_uri: str,
    status: WorkspaceStatus,
    now: datetime,
) -> None:
    workspace = await session.scalar(
        select(WorkspaceModel)
        .where(
            WorkspaceModel.tenant_id == tenant_id,
            WorkspaceModel.uri == workspace_uri,
        )
        .with_for_update()
    )
    if workspace is None:
        raise RuntimeError("Sandbox Workspace disappeared during lifecycle transition")
    current = cast(WorkspaceStatus, workspace.status)
    if current == "QUARANTINED" and status == "SEALED":
        return
    ensure_workspace_transition(current, status)
    workspace.status = status
    workspace.updated_at = now


def _parse_workspace_uri(value: str) -> WorkspaceUri:
    try:
        return WorkspaceUri.parse(value)
    except ValueError as error:
        raise _workspace_error(
            400,
            "WORKSPACE_URI_INVALID",
            "Workspace URI is invalid or non-canonical.",
        ) from error


def _workspace_record(model: WorkspaceModel) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        session_id=model.session_id,
        run_id=model.run_id,
        uri=model.uri,
        quota_bytes=model.quota_bytes,
        used_bytes=model.used_bytes,
        max_files=model.max_files,
        file_count=model.file_count,
        max_file_bytes=model.max_file_bytes,
        status=cast(WorkspaceStatus, model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
        expires_at=model.expires_at,
    )


def _workspace_error(status_code: int, code: str, message: str) -> PlatformError:
    return PlatformError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=False,
    )
