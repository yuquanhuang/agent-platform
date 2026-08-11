"""Immutable Approval facts and explicit lifecycle transitions."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

ApprovalStatus = Literal[
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
    "CONSUMED",
]
ApprovalDecision = Literal["APPROVED", "REJECTED"]

APPROVAL_STATUSES = frozenset(
    {"PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED", "CONSUMED"}
)

_APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    "PENDING": frozenset({"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}),
    "APPROVED": frozenset({"EXPIRED", "CONSUMED"}),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
    "CANCELLED": frozenset(),
    "CONSUMED": frozenset(),
}


@dataclass(frozen=True, slots=True)
class ApprovalRequestRecord:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int
    requester_id: UUID
    tool_call_id: str
    tool_name: str
    tool_schema_hash: str
    parameter_digest: str
    policy_version: str
    deployment_id: UUID
    status: ApprovalStatus
    expires_at: datetime
    resource_version: int
    self_approval_allowed: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalDecisionRecord:
    id: UUID
    tenant_id: UUID
    approval_id: UUID
    actor_id: UUID
    decision: ApprovalDecision
    comment: str | None
    created_at: datetime


def ensure_approval_transition(current: ApprovalStatus, target: ApprovalStatus) -> None:
    if target not in _APPROVAL_TRANSITIONS[current]:
        raise ValueError(f"Approval transition {current} -> {target} is not allowed")
