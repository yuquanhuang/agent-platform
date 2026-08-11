"""Tenant-isolated readers for immutable Runtime Bundle inputs."""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.generated.resource_content import (
    ResourceContentMcp,
    ResourceContentSkill,
)
from packages.contracts.public import TenantContext
from packages.domain.public import (
    BundleModelBindingSnapshotInput,
    McpCapabilitySnapshotInput,
    ResourceVersionRecord,
    ResourceVersionStatus,
)
from packages.domain.resources.model import parse_resource_content
from packages.infrastructure.database.mcp import SqlAlchemyMcpDiscoveryStore
from packages.infrastructure.database.models import (
    McpCapabilityDiscoveryModel,
    ModelBindingSnapshotModel,
    ResourceVersionModel,
    SkillSupplyChainScanModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyBundleInputReader:
    """Read immutable rows directly; never gate them on mutable Draft state."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._mcp_discoveries = SqlAlchemyMcpDiscoveryStore(session_factory)

    async def get_resource_version(
        self,
        context: TenantContext,
        *,
        resource_id: UUID,
        version_id: UUID,
    ) -> ResourceVersionRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(ResourceVersionModel).where(
                    ResourceVersionModel.tenant_id == UUID(context.tenant_id),
                    ResourceVersionModel.definition_id == resource_id,
                    ResourceVersionModel.id == version_id,
                )
            )
            if model is None:
                return None
            content = parse_resource_content(model.content_json)
            if isinstance(content, ResourceContentSkill):
                passed_scan = await unit_of_work.session.scalar(
                    select(SkillSupplyChainScanModel.id).where(
                        SkillSupplyChainScanModel.tenant_id == model.tenant_id,
                        SkillSupplyChainScanModel.published_version_id == model.id,
                        SkillSupplyChainScanModel.status == "PASSED",
                        SkillSupplyChainScanModel.content_hash == model.content_hash,
                    )
                )
                if passed_scan is None:
                    return None
            if isinstance(content, ResourceContentMcp):
                passed_discovery = await unit_of_work.session.scalar(
                    select(McpCapabilityDiscoveryModel.id).where(
                        McpCapabilityDiscoveryModel.tenant_id == model.tenant_id,
                        McpCapabilityDiscoveryModel.definition_id
                        == model.definition_id,
                        McpCapabilityDiscoveryModel.published_version_id == model.id,
                        McpCapabilityDiscoveryModel.status == "PASSED",
                        McpCapabilityDiscoveryModel.content_hash == model.content_hash,
                        McpCapabilityDiscoveryModel.capability_hash.is_not(None),
                    )
                )
                if passed_discovery is None:
                    return None
            return ResourceVersionRecord(
                id=model.id,
                tenant_id=model.tenant_id,
                definition_id=model.definition_id,
                version_no=model.version_no,
                schema_version=model.schema_version,
                content=content,
                content_hash=model.content_hash,
                release_note=model.release_note,
                status=cast(ResourceVersionStatus, model.status),
                published_at=model.published_at,
                published_by=model.published_by,
            )

    async def get_model_binding_snapshot(
        self,
        context: TenantContext,
        *,
        snapshot_id: UUID,
    ) -> BundleModelBindingSnapshotInput | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(ModelBindingSnapshotModel).where(
                    ModelBindingSnapshotModel.tenant_id == UUID(context.tenant_id),
                    ModelBindingSnapshotModel.id == snapshot_id,
                )
            )
            if model is None:
                return None
            return BundleModelBindingSnapshotInput(
                id=model.id,
                tenant_id=model.tenant_id,
                model_config_version_id=model.model_config_version_id,
                snapshot_hash=model.snapshot_hash,
            )

    async def get_mcp_capability_snapshot(
        self,
        context: TenantContext,
        *,
        published_version_id: UUID,
    ) -> McpCapabilitySnapshotInput | None:
        return await self._mcp_discoveries.get_capability_snapshot(
            context, published_version_id=published_version_id
        )
