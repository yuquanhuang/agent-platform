"""Deployment identity, compatibility, and lifecycle rules."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

DeploymentStatus = Literal["STAGED", "ACTIVE", "DEGRADED", "RETIRED", "FAILED"]

_ALLOWED_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    "STAGED": frozenset({"ACTIVE", "FAILED"}),
    "ACTIVE": frozenset({"DEGRADED", "RETIRED"}),
    "DEGRADED": frozenset({"ACTIVE", "RETIRED"}),
    "RETIRED": frozenset(),
    "FAILED": frozenset(),
}


class DeploymentTransitionError(ValueError):
    """Reject a state change outside the frozen Deployment lifecycle."""


def ensure_deployment_transition(
    current: DeploymentStatus, target: DeploymentStatus
) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise DeploymentTransitionError(
            f"invalid Deployment transition: {current} -> {target}"
        )


def deployment_id(tenant_id: UUID, release_id: UUID, runtime_target_id: str) -> UUID:
    """Return the stable identity used by at-least-once activation retries."""

    return uuid5(
        NAMESPACE_URL,
        f"deployment/{tenant_id}/{release_id}/{runtime_target_id}",
    )


def deployment_compatibility_hash(
    *,
    agent_id: UUID,
    snapshot_id: UUID,
    bundle_id: UUID,
    bundle_content_hash: str,
    runtime_target_id: str,
    runtime_type: str,
    runtime_image_digest: str,
) -> str:
    """Hash immutable runtime inputs that govern session compatibility."""

    payload = {
        "schema_version": "deployment-compatibility/v1",
        "agent_id": str(agent_id),
        "snapshot_id": str(snapshot_id),
        "bundle_id": str(bundle_id),
        "bundle_content_hash": bundle_content_hash,
        "runtime_target_id": runtime_target_id,
        "runtime_type": runtime_type,
        "runtime_image_digest": runtime_image_digest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    id: UUID
    tenant_id: UUID
    release_id: UUID
    agent_id: UUID
    snapshot_id: UUID
    bundle_id: UUID
    runtime_target_id: str
    status: DeploymentStatus
    compatibility_hash: str
    activation_fencing_token: int
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
