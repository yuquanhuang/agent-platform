"""Application boundary for immutable Agent Snapshot compilation."""

from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    AgentSnapshotRecord,
    AgentVersionRecord,
    SnapshotPublicationRecord,
    TenantAccess,
)


class SnapshotAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class SnapshotCompilationStore(Protocol):
    async def get_snapshot(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> AgentSnapshotRecord | None: ...

    async def get_version(
        self, context: TenantContext, *, agent_version_id: UUID
    ) -> AgentVersionRecord | None: ...

    async def compile_snapshot(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_draft_resource_version: int,
        release_note: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> SnapshotPublicationRecord | None: ...


class SnapshotReader(Protocol):
    async def get_snapshot(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> AgentSnapshotRecord | None: ...

    async def get_version(
        self, context: TenantContext, *, agent_version_id: UUID
    ) -> AgentVersionRecord | None: ...


class SnapshotCompilationService:
    """Authorize publication and delegate one consistent compilation transaction."""

    def __init__(
        self,
        access_resolver: SnapshotAccessResolver,
        store: SnapshotCompilationStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def compile_snapshot(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        expected_draft_resource_version: int,
        release_note: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> SnapshotPublicationRecord:
        if expected_draft_resource_version < 1:
            raise validation_error("Expected Agent Draft version must be positive.")
        if not release_note or len(release_note) > 2000:
            raise validation_error(
                "Agent release_note must contain between 1 and 2000 characters."
            )
        try:
            parsed_agent_id = UUID(agent_id)
        except ValueError as exc:
            raise validation_error(
                "agent_id must be a valid resource identifier."
            ) from exc
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "publish"):
            raise permission_denied()
        result = await self._store.compile_snapshot(
            access.context,
            actor_id=UUID(access.context.subject_id),
            agent_id=parsed_agent_id,
            expected_draft_resource_version=expected_draft_resource_version,
            release_note=release_note,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "agent.snapshot.compile",
                extra={
                    "agent_id": agent_id,
                    "expected_draft_resource_version": (
                        expected_draft_resource_version
                    ),
                    "release_note": release_note,
                },
            ),
            metadata=metadata,
        )
        if result is None:
            raise resource_not_found()
        return result
