"""PostgreSQL adapters for trusted Skill Artifacts and scan evidence."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import (
    RequestMetadata,
    TrustedSkillArtifactContentReader,
)
from packages.contracts.public import TenantContext
from packages.domain.public import (
    ArtifactStatus,
    SkillArtifactPayload,
    SkillScanEvidenceRecord,
    SkillSupplyChainScanResult,
    artifact_uri,
)
from packages.domain.skills.model import SkillScanStatus
from packages.infrastructure.database.models import (
    ArtifactModel,
    AuditLogModel,
    SkillSupplyChainScanModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_READ_CONCURRENCY = 8


class SqlAlchemySkillArtifactReader:
    """Batch metadata lookup plus bounded reads from canonical trusted storage."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        content_reader: TrustedSkillArtifactContentReader,
    ) -> None:
        self._session_factory = session_factory
        self._content_reader = content_reader

    async def load_artifacts(
        self,
        context: TenantContext,
        *,
        owner_user_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> dict[UUID, SkillArtifactPayload]:
        if not artifact_ids:
            return {}
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            rows = list(
                (
                    await unit_of_work.session.scalars(
                        select(ArtifactModel).where(
                            ArtifactModel.tenant_id == tenant_id,
                            ArtifactModel.owner_user_id == owner_user_id,
                            ArtifactModel.id.in_(artifact_ids),
                        )
                    )
                ).all()
            )
        semaphore = asyncio.Semaphore(_READ_CONCURRENCY)

        async def load(model: ArtifactModel) -> SkillArtifactPayload | None:
            content = b""
            if model.status == "AVAILABLE" and model.object_uri is not None:
                if model.object_uri != artifact_uri(
                    tenant_id=tenant_id, artifact_id=model.id
                ):
                    return None
                async with semaphore:
                    loaded = await self._content_reader.read_trusted_artifact(
                        context,
                        artifact_id=model.id,
                        object_uri=model.object_uri,
                        max_bytes=min(model.size_bytes + 1, _MAX_ARTIFACT_BYTES + 1),
                    )
                if loaded is None:
                    return None
                content = loaded
            return SkillArtifactPayload(
                artifact_id=model.id,
                tenant_id=model.tenant_id,
                owner_user_id=model.owner_user_id,
                status=cast(ArtifactStatus, model.status),
                content_hash=model.content_hash,
                size_bytes=model.size_bytes,
                content_type=model.content_type,
                expires_at=model.expires_at,
                content=content,
            )

        loaded = await asyncio.gather(*(load(row) for row in rows))
        return {
            artifact.artifact_id: artifact
            for artifact in loaded
            if artifact is not None
        }


class SqlAlchemySkillScanStore:
    """Append terminal scan evidence without persisting Skill file contents."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_scan(
        self,
        context: TenantContext,
        *,
        definition_id: UUID,
        draft_resource_version: int,
        content_hash: str,
        result: SkillSupplyChainScanResult,
        scanned_by: UUID,
        metadata: RequestMetadata,
    ) -> SkillScanEvidenceRecord:
        now = datetime.now(UTC)
        tenant_id = UUID(context.tenant_id)
        model = SkillSupplyChainScanModel(
            id=uuid4(),
            tenant_id=tenant_id,
            definition_id=definition_id,
            draft_resource_version=draft_resource_version,
            content_hash=content_hash,
            scanner_name=result.scanner_name,
            scanner_version=result.scanner_version,
            policy_version=result.policy_version,
            status=result.status,
            findings_json=cast(list[dict[str, object]], list(result.findings)),
            report_hash=result.report_hash,
            sbom_json=cast(dict[str, object], result.sbom),
            sbom_hash=result.sbom_hash,
            signature_status=result.signature_status,
            provenance_status=result.provenance_status,
            scanned_at=now,
            scanned_by=scanned_by,
        )
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            session.add(model)
            await session.flush()
            await _audit_scan(session, model=model, metadata=metadata)
        return _scan_record(model)


def _scan_record(model: SkillSupplyChainScanModel) -> SkillScanEvidenceRecord:
    return SkillScanEvidenceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        definition_id=model.definition_id,
        draft_resource_version=model.draft_resource_version,
        content_hash=model.content_hash,
        status=cast(SkillScanStatus, model.status),
        report_hash=model.report_hash,
        published_version_id=model.published_version_id,
        scanned_at=model.scanned_at,
        scanned_by=model.scanned_by,
    )


async def _audit_scan(
    session: AsyncSession,
    *,
    model: SkillSupplyChainScanModel,
    metadata: RequestMetadata,
) -> None:
    finding_codes = [
        code
        for finding in model.findings_json
        if isinstance((code := finding.get("code")), str)
    ]
    summary: dict[str, object] = {
        "scan_id": str(model.id),
        "draft_resource_version": model.draft_resource_version,
        "content_hash": model.content_hash,
        "status": model.status,
        "scanner": model.scanner_name,
        "scanner_version": model.scanner_version,
        "policy_version": model.policy_version,
        "report_hash": model.report_hash,
        "sbom_hash": model.sbom_hash,
        "finding_codes": finding_codes,
    }
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            id=uuid4(),
            tenant_id=model.tenant_id,
            actor_type="user",
            actor_id=model.scanned_by,
            action="skill.supply_chain_scan",
            resource_type="skill",
            resource_id=model.definition_id,
            result="SUCCESS" if model.status == "PASSED" else "FAILED",
            reason_codes=finding_codes,
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_schema_version=1,
            metadata_json=summary,
            created_at=model.scanned_at,
        )
    )
