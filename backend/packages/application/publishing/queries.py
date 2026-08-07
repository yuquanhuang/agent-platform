"""Read-only publication preview, version history and Snapshot diff use cases."""

from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.application.publishing.releases import normalize_runtime_targets
from packages.contracts.generated.core_models import (
    AgentVersion,
    AgentVersionPage,
    PublishAgentPreview,
    PublishAgentPreviewRequest,
    PublishAgentPreviewTarget,
    ResolvedPublishBinding,
    SnapshotDiff,
    SnapshotDiffChangesItem,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    AgentVersionSnapshotRecord,
    PublishPreviewRecord,
    SnapshotChangeRecord,
    TenantAccess,
)


class PublicationQueryAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class PublicationQueryStore(Protocol):
    async def list_agent_versions(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[AgentVersionSnapshotRecord], str | None] | None: ...

    async def get_agent_version(
        self, context: TenantContext, *, agent_id: UUID, version_id: UUID
    ) -> AgentVersionSnapshotRecord | None: ...

    async def diff_agent_snapshots(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        from_snapshot_id: UUID,
        to_snapshot_id: UUID,
    ) -> tuple[SnapshotChangeRecord, ...] | None: ...

    async def preview_agent_publish(
        self,
        context: TenantContext,
        *,
        agent_id: UUID,
        expected_agent_version: int,
        runtime_targets: tuple[str, ...],
    ) -> PublishPreviewRecord | None: ...


class PublicationQueryService:
    """Authorize immutable history reads and side-effect-free publication preview."""

    def __init__(
        self,
        access_resolver: PublicationQueryAccessResolver,
        store: PublicationQueryStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_agent_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> AgentVersionPage:
        access = await self._read_access(principal, metadata)
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200.")
        result = await self._store.list_agent_versions(
            access.context,
            agent_id=_uuid(agent_id, "agent_id"),
            limit=limit,
            cursor=cursor,
        )
        if result is None:
            raise resource_not_found()
        records, next_cursor = result
        return AgentVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def get_agent_version(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        version_id: str,
        metadata: RequestMetadata,
    ) -> AgentVersion:
        access = await self._read_access(principal, metadata)
        record = await self._store.get_agent_version(
            access.context,
            agent_id=_uuid(agent_id, "agent_id"),
            version_id=_uuid(version_id, "version_id"),
        )
        if record is None:
            raise resource_not_found()
        return _version(record)

    async def diff_agent_snapshots(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        from_snapshot_id: str,
        to_snapshot_id: str,
        metadata: RequestMetadata,
    ) -> SnapshotDiff:
        access = await self._read_access(principal, metadata)
        parsed_agent_id = _uuid(agent_id, "agent_id")
        parsed_from = _uuid(from_snapshot_id, "from_snapshot_id")
        parsed_to = _uuid(to_snapshot_id, "to_snapshot_id")
        changes = await self._store.diff_agent_snapshots(
            access.context,
            agent_id=parsed_agent_id,
            from_snapshot_id=parsed_from,
            to_snapshot_id=parsed_to,
        )
        if changes is None:
            raise resource_not_found()
        return SnapshotDiff(
            agent_id=str(parsed_agent_id),
            from_snapshot_id=str(parsed_from),
            to_snapshot_id=str(parsed_to),
            changes=[_change(item) for item in changes],
        )

    async def preview_agent_publish(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        request: PublishAgentPreviewRequest,
        metadata: RequestMetadata,
    ) -> PublishAgentPreview:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "publish"):
            raise permission_denied()
        record = await self._store.preview_agent_publish(
            access.context,
            agent_id=_uuid(agent_id, "agent_id"),
            expected_agent_version=request.expected_agent_version,
            runtime_targets=normalize_runtime_targets(request.runtime_targets),
        )
        if record is None:
            raise resource_not_found()
        return PublishAgentPreview(
            agent_id=str(record.agent_id),
            expected_agent_version=record.expected_agent_version,
            preview_snapshot_hash=record.preview_snapshot_hash,
            resolved_bindings=[
                ResolvedPublishBinding(
                    resource_type=item.resource_type,
                    resource_id=str(item.resource_id),
                    version_id=str(item.version_id),
                    version_no=item.version_no,
                    content_hash=item.content_hash,
                    binding_role=item.binding_role,
                )
                for item in record.resolved_bindings
            ],
            targets=[
                PublishAgentPreviewTarget(
                    runtime_target_id=target.runtime_target_id,
                    current_deployment_id=(
                        str(target.current_deployment_id)
                        if target.current_deployment_id is not None
                        else None
                    ),
                    current_snapshot_id=(
                        str(target.current_snapshot_id)
                        if target.current_snapshot_id is not None
                        else None
                    ),
                    changes=[_change(item) for item in target.changes],
                )
                for target in record.targets
            ],
            ready_to_publish=record.ready_to_publish,
        )

    async def _read_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "read"):
            raise permission_denied()
        return access


def _version(record: AgentVersionSnapshotRecord) -> AgentVersion:
    return AgentVersion(
        id=str(record.version.id),
        agent_id=str(record.version.agent_id),
        version_no=record.version.version_no,
        snapshot_id=str(record.snapshot.id),
        content_hash=record.snapshot.content_hash,
        release_note=record.version.release_note,
        created_at=record.version.created_at,
    )


def _change(record: SnapshotChangeRecord) -> SnapshotDiffChangesItem:
    return SnapshotDiffChangesItem(
        category=record.category,
        path=record.path,
        change_type=record.change_type,
        before=record.before,
        after=record.after,
        sensitive=record.sensitive,
    )


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error(f"{field} must be a valid resource identifier.") from exc
