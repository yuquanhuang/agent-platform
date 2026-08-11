"""Artifact identity, scan facts, and lifecycle transition rules."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

ArtifactStatus = Literal[
    "UPLOADING",
    "SCANNING",
    "AVAILABLE",
    "REJECTED",
    "FAILED",
    "EXPIRED",
    "DELETING",
    "DELETED",
]

ARTIFACT_STATUSES = frozenset(
    {
        "UPLOADING",
        "SCANNING",
        "AVAILABLE",
        "REJECTED",
        "FAILED",
        "EXPIRED",
        "DELETING",
        "DELETED",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: UUID
    tenant_id: UUID
    workspace_id: UUID | None
    run_id: UUID | None
    owner_user_id: UUID
    name: str
    quarantine_object_uri: str
    object_uri: str | None
    content_hash: str
    size_bytes: int
    content_type: str
    status: ArtifactStatus
    required_output: bool
    scan_result: dict[str, JsonValue] | None
    upload_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    deleted_at: datetime | None


def ensure_artifact_transition(current: ArtifactStatus, target: ArtifactStatus) -> None:
    if current == target:
        return
    allowed: dict[ArtifactStatus, frozenset[ArtifactStatus]] = {
        "UPLOADING": frozenset({"SCANNING", "FAILED"}),
        "SCANNING": frozenset({"AVAILABLE", "REJECTED", "FAILED"}),
        "AVAILABLE": frozenset({"EXPIRED", "DELETING"}),
        "REJECTED": frozenset({"DELETING"}),
        "FAILED": frozenset({"DELETING"}),
        "EXPIRED": frozenset({"DELETING"}),
        "DELETING": frozenset({"DELETED"}),
        "DELETED": frozenset(),
    }
    if target not in allowed[current]:
        raise ValueError(f"Artifact transition {current} -> {target} is not allowed")


def artifact_uri(*, tenant_id: UUID, artifact_id: UUID) -> str:
    return f"artifact://tenant/{tenant_id}/artifact/{artifact_id}"
