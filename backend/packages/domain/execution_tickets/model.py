"""Immutable one-time authorization bound to one exact tool invocation."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ExecutionTicketRecord:
    id: UUID
    tenant_id: UUID
    approval_id: UUID
    run_id: UUID
    execution_attempt: int
    requester_id: UUID
    tool_name: str
    tool_schema_hash: str
    parameter_digest: str
    policy_version: str
    deployment_id: UUID
    nonce_hash: str
    expires_at: datetime
    single_use: bool
    consumed_at: datetime | None
    created_at: datetime
