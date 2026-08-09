"""Immutable Run identity facts and explicit lifecycle transitions."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

RunStatus = Literal[
    "CREATED",
    "QUEUED",
    "PREPARING",
    "RUNNING",
    "WAITING_APPROVAL",
    "CANCELLING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
]
RunAttemptStatus = Literal[
    "ALLOCATED", "STARTING", "RUNNING", "COMPLETED", "LOST", "CANCELLED"
]

RUN_STATUSES = frozenset(
    {
        "CREATED",
        "QUEUED",
        "PREPARING",
        "RUNNING",
        "WAITING_APPROVAL",
        "CANCELLING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
    }
)
TERMINAL_RUN_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"})
NON_TERMINAL_RUN_STATUSES = RUN_STATUSES - TERMINAL_RUN_STATUSES
RUN_ATTEMPT_STATUSES = frozenset(
    {"ALLOCATED", "STARTING", "RUNNING", "COMPLETED", "LOST", "CANCELLED"}
)

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    "CREATED": frozenset({"QUEUED", "CANCELLING", "FAILED"}),
    "QUEUED": frozenset({"PREPARING", "CANCELLING", "TIMEOUT"}),
    "PREPARING": frozenset({"RUNNING", "CANCELLING", "FAILED", "TIMEOUT"}),
    "RUNNING": frozenset(
        {"WAITING_APPROVAL", "CANCELLING", "SUCCEEDED", "FAILED", "TIMEOUT"}
    ),
    "WAITING_APPROVAL": frozenset({"RUNNING", "CANCELLING", "TIMEOUT"}),
    "CANCELLING": frozenset({"CANCELLED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
    "TIMEOUT": frozenset(),
}
_ATTEMPT_TRANSITIONS: dict[RunAttemptStatus, frozenset[RunAttemptStatus]] = {
    "ALLOCATED": frozenset({"STARTING", "LOST", "CANCELLED"}),
    "STARTING": frozenset({"RUNNING", "LOST", "CANCELLED"}),
    "RUNNING": frozenset({"COMPLETED", "LOST", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "LOST": frozenset(),
    "CANCELLED": frozenset(),
}


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    tenant_id: UUID
    session_id: UUID
    branch_id: UUID | None
    user_message_id: UUID
    assistant_message_id: UUID | None
    agent_id: UUID
    snapshot_id: UUID
    deployment_id: UUID
    status: RunStatus
    result_quality: Literal["NORMAL", "SUCCEEDED_WITH_WARNINGS"] | None
    current_attempt: int
    latest_sequence_no: int
    idempotency_key: str
    client_request_id: str | None
    retry_of_run_id: UUID | None
    timeout_seconds: int
    token_budget: int | None
    cost_budget_amount: Decimal | None
    cost_budget_currency: str | None
    workflow_id: str | None
    error_code: str | None
    error_detail: dict[str, JsonValue] | None
    created_by: UUID
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunAttemptRecord:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    attempt_no: int
    fencing_token_hash: str
    worker_id: str | None
    runtime_handle_ref: str | None
    status: RunAttemptStatus
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    error_code: str | None


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in _RUN_TRANSITIONS[current]:
        raise ValueError(f"Run transition {current} -> {target} is not allowed")


def ensure_run_attempt_transition(
    current: RunAttemptStatus, target: RunAttemptStatus
) -> None:
    if target not in _ATTEMPT_TRANSITIONS[current]:
        raise ValueError(f"RunAttempt transition {current} -> {target} is not allowed")
