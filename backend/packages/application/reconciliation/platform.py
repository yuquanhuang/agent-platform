"""One bounded reconciliation pass across durable platform components."""

from dataclasses import dataclass, field
from datetime import datetime

from packages.application.reconciliation.approvals import (
    ApprovalReconciler,
    ApprovalReconciliationSummary,
)
from packages.application.reconciliation.runs import (
    RunReconciler,
    RunReconciliationSummary,
)
from packages.application.reconciliation.sandboxes import (
    SandboxReconciler,
    SandboxReconciliationSummary,
)
from packages.contracts.public import TenantContext


@dataclass(frozen=True, slots=True)
class PlatformReconciliationSummary:
    runs: RunReconciliationSummary = field(default_factory=RunReconciliationSummary)
    approvals: ApprovalReconciliationSummary = field(
        default_factory=ApprovalReconciliationSummary
    )
    sandboxes: SandboxReconciliationSummary = field(
        default_factory=SandboxReconciliationSummary
    )


class PlatformReconciler:
    """Keep component policies explicit while sharing tenant and cycle time."""

    def __init__(
        self,
        runs: RunReconciler,
        approvals: ApprovalReconciler,
        sandboxes: SandboxReconciler,
    ) -> None:
        self._runs = runs
        self._approvals = approvals
        self._sandboxes = sandboxes

    async def reconcile_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> PlatformReconciliationSummary:
        return PlatformReconciliationSummary(
            runs=await self._runs.reconcile_tenant_once(context, now=now),
            approvals=await self._approvals.reconcile_tenant_once(context, now=now),
            sandboxes=await self._sandboxes.reconcile_tenant_once(context, now=now),
        )

    async def process_run_admission_queue(
        self, context: TenantContext, *, now: datetime
    ) -> RunReconciliationSummary:
        """Run the latency-sensitive scheduler without scanning other resources."""

        return await self._runs.process_admission_queue(context, now=now)

    async def process_capacity_admission_domains(
        self, scheduler_context: TenantContext, *, now: datetime
    ) -> RunReconciliationSummary:
        """Run the cross-tenant durable Capacity Domain scheduler."""

        return await self._runs.process_capacity_admission_domains(
            scheduler_context, now=now
        )
