"""Durable tenant quota policy records."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

QuotaPolicyStatus = Literal["ACTIVE", "DISABLED"]
BudgetPolicyStatus = Literal["ACTIVE", "DISABLED"]
StoragePolicyStatus = Literal["ACTIVE", "DISABLED"]
BudgetPeriod = Literal["DAILY", "MONTHLY"]
BudgetEnforcement = Literal["HARD", "SOFT"]


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


@dataclass(frozen=True, slots=True)
class BudgetPolicyVersionRecord:
    id: UUID
    tenant_id: UUID
    policy_id: UUID
    version_no: int
    period: BudgetPeriod
    enforcement: BudgetEnforcement
    token_limit: int
    cost_limit_amount: Decimal | None
    cost_limit_currency: str | None
    price_catalog_version: str | None
    content_hash: str
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetPolicyRecord:
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    status: BudgetPolicyStatus
    current_version: BudgetPolicyVersionRecord
    resource_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StorageLimitsRecord:
    max_reserved_workspace_bytes: int | None = None
    max_reserved_workspaces: int | None = None
    max_reserved_artifact_bytes: int | None = None
    max_reserved_artifacts: int | None = None


@dataclass(frozen=True, slots=True)
class StoragePolicyVersionRecord:
    id: UUID
    tenant_id: UUID
    policy_id: UUID
    version_no: int
    limits: StorageLimitsRecord
    content_hash: str
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoragePolicyRecord:
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    status: StoragePolicyStatus
    current_version: StoragePolicyVersionRecord
    resource_version: int
    created_at: datetime
    updated_at: datetime
