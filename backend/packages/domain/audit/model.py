"""Immutable, tenant-scoped audit query facts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

AuditResult = Literal["SUCCESS", "DENIED", "FAILED"]


@dataclass(frozen=True, slots=True)
class AuditLogRecord:
    event_id: UUID
    tenant_id: UUID | None
    occurred_at: datetime
    actor_type: str
    actor_id: UUID
    action: str
    resource_type: str
    resource_id: UUID | None
    run_id: UUID | None
    result: AuditResult
    trace_id: str
