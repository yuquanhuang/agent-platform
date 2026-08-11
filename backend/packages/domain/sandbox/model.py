"""Immutable Sandbox facts and the allowed lifecycle graph."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

SandboxStatus = Literal[
    "REQUESTED",
    "PROVISIONING",
    "READY",
    "IN_USE",
    "FAILED",
    "QUARANTINED",
    "TERMINATING",
    "TERMINATED",
]

SANDBOX_STATUSES = frozenset(
    {
        "REQUESTED",
        "PROVISIONING",
        "READY",
        "IN_USE",
        "FAILED",
        "QUARANTINED",
        "TERMINATING",
        "TERMINATED",
    }
)

_TRANSITIONS: dict[SandboxStatus, frozenset[SandboxStatus]] = {
    "REQUESTED": frozenset({"PROVISIONING", "FAILED", "TERMINATING"}),
    "PROVISIONING": frozenset({"READY", "FAILED", "QUARANTINED", "TERMINATING"}),
    "READY": frozenset({"IN_USE", "QUARANTINED", "TERMINATING"}),
    "IN_USE": frozenset({"READY", "QUARANTINED", "TERMINATING"}),
    "FAILED": frozenset({"TERMINATING", "TERMINATED"}),
    "QUARANTINED": frozenset({"TERMINATING"}),
    "TERMINATING": frozenset({"TERMINATED", "QUARANTINED"}),
    "TERMINATED": frozenset(),
}


@dataclass(frozen=True, slots=True)
class SandboxInstanceRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    session_id: UUID
    run_id: UUID
    execution_attempt: int
    scope: Literal["run", "session"]
    image_digest: str
    policy_ref: str
    policy_hash: str
    policy_schema_version: str
    policy_json: dict[str, object]
    bundle_ref: str
    bundle_hash: str
    workspace_uri: str
    runtime_target_id: str | None
    status: SandboxStatus
    provider_ref: str | None
    lease_expires_at: datetime | None
    provision_operation_id: UUID
    created_at: datetime
    updated_at: datetime
    terminated_at: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class SandboxLeaseRecord:
    id: UUID
    tenant_id: UUID
    sandbox_id: UUID
    holder_run_id: UUID
    execution_attempt: int
    fencing_token_hash: str
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None


def ensure_sandbox_transition(current: SandboxStatus, target: SandboxStatus) -> None:
    if current == target:
        return
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"Sandbox transition {current} -> {target} is not allowed")
