"""Release lifecycle and immutable Runtime Bundle persistence records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

ReleaseStatus = Literal[
    "REQUESTED",
    "VALIDATING",
    "COMPILING",
    "SCANNING",
    "SMOKE_TESTING",
    "ACTIVATING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]
ReleaseKind = Literal["PUBLISH", "ROLLBACK"]
RuntimeBundleScanStatus = Literal["PENDING", "PASSED", "FAILED"]

_ALLOWED_TRANSITIONS: dict[ReleaseStatus, frozenset[ReleaseStatus]] = {
    "REQUESTED": frozenset({"VALIDATING", "CANCELLED"}),
    "VALIDATING": frozenset({"COMPILING", "FAILED", "CANCELLED"}),
    "COMPILING": frozenset({"SCANNING", "FAILED", "CANCELLED"}),
    "SCANNING": frozenset({"SMOKE_TESTING", "ACTIVATING", "FAILED", "CANCELLED"}),
    "SMOKE_TESTING": frozenset({"ACTIVATING", "FAILED", "CANCELLED"}),
    "ACTIVATING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


class ReleaseTransitionError(ValueError):
    """Reject a state change that is not part of the frozen Release lifecycle."""


def ensure_release_transition(current: ReleaseStatus, target: ReleaseStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ReleaseTransitionError(
            f"invalid Release transition: {current} -> {target}"
        )


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    requested_by: UUID
    operation_id: UUID
    release_kind: ReleaseKind
    expected_agent_version: int | None
    requested_snapshot_id: UUID | None
    runtime_targets: tuple[str, ...]
    release_note: str
    run_smoke_test: bool
    activate_on_success: bool
    status: ReleaseStatus
    workflow_id: str
    snapshot_id: UUID | None
    deployment_ids: tuple[UUID, ...]
    error_code: str | None
    error_detail: dict[str, JsonValue] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeBundleRecord:
    id: UUID
    tenant_id: UUID
    snapshot_id: UUID
    runtime_type: str
    compiler_name: str
    compiler_version: str
    manifest_schema_version: str
    manifest: dict[str, JsonValue]
    content_hash: str
    object_uri: str
    size_bytes: int
    signature_ref: str | None
    sbom_ref: str | None
    scan_status: RuntimeBundleScanStatus
    created_at: datetime
