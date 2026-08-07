"""Frozen getDeployment authorization and response mapping."""

from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import Deployment
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import DeploymentRecord, TenantAccess


class DeploymentAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class DeploymentStore(Protocol):
    async def get_deployment(
        self, context: TenantContext, *, deployment_id: UUID
    ) -> DeploymentRecord | None: ...


class DeploymentManagementService:
    """Authorize tenant-scoped Deployment history reads."""

    def __init__(
        self, access_resolver: DeploymentAccessResolver, store: DeploymentStore
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def get_deployment(
        self,
        principal: AuthenticatedPrincipal,
        *,
        deployment_id: str,
        metadata: RequestMetadata,
    ) -> Deployment:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "read"):
            raise permission_denied()
        record = await self._store.get_deployment(
            access.context,
            deployment_id=_uuid(deployment_id),
        )
        if record is None:
            raise resource_not_found()
        return Deployment(
            id=str(record.id),
            agent_id=str(record.agent_id),
            snapshot_id=str(record.snapshot_id),
            bundle_id=str(record.bundle_id),
            runtime_target_id=record.runtime_target_id,
            status=record.status,
            compatibility_hash=record.compatibility_hash,
            created_at=record.created_at,
            activated_at=record.activated_at,
        )


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error(
            "deployment_id must be a valid resource identifier."
        ) from exc
