"""Immutable Skill package facts used by import and publication."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from packages.contracts.generated.resource_content import ResourceContentSkill
from packages.domain.artifacts.model import ArtifactStatus

SkillScanStatus = Literal["PASSED", "REJECTED", "FAILED"]
SkillVerificationStatus = Literal["VERIFIED", "UNVERIFIED", "NOT_PROVIDED"]


@dataclass(frozen=True, slots=True)
class SkillArtifactPayload:
    """Tenant/owner-scoped immutable Artifact metadata and trusted bytes."""

    artifact_id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    status: ArtifactStatus
    content_hash: str
    size_bytes: int
    content_type: str
    expires_at: datetime
    content: bytes


@dataclass(frozen=True, slots=True)
class ValidatedSkillPackage:
    """A frozen Skill draft whose Artifact bytes match every declared hash."""

    content: ResourceContentSkill
    content_hash: str
    files: tuple[tuple[str, SkillArtifactPayload], ...]


@dataclass(frozen=True, slots=True)
class SkillSupplyChainScanResult:
    """Terminal scanner result persisted before publication is attempted."""

    status: SkillScanStatus
    scanner_name: str
    scanner_version: str
    policy_version: str
    findings: tuple[dict[str, JsonValue], ...]
    report_hash: str
    sbom: dict[str, JsonValue]
    sbom_hash: str
    signature_status: SkillVerificationStatus
    provenance_status: SkillVerificationStatus


@dataclass(frozen=True, slots=True)
class SkillScanEvidenceRecord:
    """Durable evidence bound once to an immutable Resource Version."""

    id: UUID
    tenant_id: UUID
    definition_id: UUID
    draft_resource_version: int
    content_hash: str
    status: SkillScanStatus
    report_hash: str
    published_version_id: UUID | None
    scanned_at: datetime
    scanned_by: UUID
