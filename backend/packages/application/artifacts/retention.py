"""Internal Artifact retention and legal-hold management boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from packages.contracts.public import TenantContext


@dataclass(frozen=True, slots=True)
class ArtifactRetentionPolicy:
    """Deployment defaults frozen into each Artifact lifecycle fact."""

    available_retention: timedelta = timedelta(days=30)
    forensic_retention: timedelta = timedelta(days=7)
    delete_recovery_delay: timedelta = timedelta(hours=1)
    delete_recovery_max_operations: int = 3

    def __post_init__(self) -> None:
        if self.available_retention <= timedelta(0):
            raise ValueError("Artifact available retention must be positive")
        if self.forensic_retention <= timedelta(0):
            raise ValueError("Artifact forensic retention must be positive")
        if self.delete_recovery_delay <= timedelta(0):
            raise ValueError("Artifact delete recovery delay must be positive")
        if self.delete_recovery_max_operations < 1:
            raise ValueError(
                "Artifact delete recovery operation limit must be positive"
            )


@dataclass(frozen=True, slots=True)
class ArtifactLegalHoldRecord:
    id: UUID
    tenant_id: UUID
    artifact_id: UUID
    case_ref: str
    reason: str
    placed_by: UUID
    placed_at: datetime
    released_by: UUID | None
    released_at: datetime | None


class ArtifactRetentionAdministrationStore(Protocol):
    async def place_legal_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord: ...

    async def release_legal_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord | None: ...

    async def retry_failed_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        reason: str,
        now: datetime,
    ) -> UUID | None: ...


class ArtifactRetentionAdministrationService:
    """Audited internal control plane; intentionally not exposed through OpenAPI."""

    def __init__(self, store: ArtifactRetentionAdministrationStore) -> None:
        self._store = store

    async def place_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord:
        _validate_hold_text(case_ref=case_ref, reason=reason)
        return await self._store.place_legal_hold(
            context,
            artifact_id=artifact_id,
            case_ref=case_ref.strip(),
            reason=reason.strip(),
            now=now,
        )

    async def release_hold(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        case_ref: str,
        reason: str,
        now: datetime,
    ) -> ArtifactLegalHoldRecord | None:
        _validate_hold_text(case_ref=case_ref, reason=reason)
        return await self._store.release_legal_hold(
            context,
            artifact_id=artifact_id,
            case_ref=case_ref.strip(),
            reason=reason.strip(),
            now=now,
        )

    async def retry_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        reason: str,
        now: datetime,
    ) -> UUID | None:
        if not reason.strip() or len(reason.strip()) > 2000:
            raise ValueError("Artifact delete retry reason must be 1-2000 characters")
        return await self._store.retry_failed_delete(
            context,
            artifact_id=artifact_id,
            reason=reason.strip(),
            now=now,
        )


def _validate_hold_text(*, case_ref: str, reason: str) -> None:
    if not case_ref.strip() or len(case_ref.strip()) > 255:
        raise ValueError("Artifact legal-hold case_ref must be 1-255 characters")
    if not reason.strip() or len(reason.strip()) > 2000:
        raise ValueError("Artifact legal-hold reason must be 1-2000 characters")
