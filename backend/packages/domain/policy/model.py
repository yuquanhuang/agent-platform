"""Durable tenant quota policy records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

QuotaPolicyStatus = Literal["ACTIVE", "DISABLED"]


@dataclass(frozen=True, slots=True)
class RunCapacityLimitsRecord:
    max_nonterminal_runs_per_tenant: int | None = None
    max_nonterminal_runs_per_user: int | None = None
    max_nonterminal_runs_per_agent: int | None = None
    max_nonterminal_agentscope_runs: int | None = None
    max_nonterminal_codex_runs: int | None = None


@dataclass(frozen=True, slots=True)
class QuotaPolicyVersionRecord:
    id: UUID
    tenant_id: UUID
    policy_id: UUID
    version_no: int
    limits: RunCapacityLimitsRecord
    content_hash: str
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaPolicyRecord:
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    status: QuotaPolicyStatus
    current_version: QuotaPolicyVersionRecord
    resource_version: int
    created_at: datetime
    updated_at: datetime
