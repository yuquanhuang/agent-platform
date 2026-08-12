"""Deterministic Artifact integrity scanner used before trusted promotion."""

from datetime import UTC, datetime

from packages.application.artifacts import (
    ArtifactObjectNotFound,
    ArtifactObjectStore,
    ArtifactObjectStoreUnavailable,
    ArtifactScanVerdict,
    RetryableArtifactScanError,
)
from packages.contracts.public import TenantContext
from packages.domain.public import ArtifactRecord


class ObjectIntegrityArtifactSecurityScanner:
    """Revalidate immutable size, SHA-256 and MIME facts before promotion."""

    def __init__(self, objects: ArtifactObjectStore) -> None:
        self._objects = objects

    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict:
        try:
            observation = await self._objects.inspect_quarantine(
                context, artifact=artifact
            )
        except (ArtifactObjectNotFound, ArtifactObjectStoreUnavailable) as error:
            raise RetryableArtifactScanError(
                "Artifact integrity source is unavailable"
            ) from error
        findings: list[str] = []
        if observation.size_bytes != artifact.size_bytes:
            findings.append("ARTIFACT_SIZE_MISMATCH")
        if observation.content_hash != artifact.content_hash:
            findings.append("ARTIFACT_HASH_MISMATCH")
        if observation.content_type != artifact.content_type:
            findings.append("ARTIFACT_CONTENT_TYPE_MISMATCH")
        return ArtifactScanVerdict(
            decision="REJECTED" if findings else "PASSED",
            engine="object-integrity",
            definition_version="sha256-mime-v1",
            findings=tuple(findings),
            scanned_at=datetime.now(UTC),
        )
