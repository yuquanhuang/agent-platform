"""Explicit production composition for the platform reconciliation worker."""

from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from packages.application.policy import RunCapacityPolicy
from packages.application.reconciliation import (
    ApprovalReconciler,
    PlatformReconciler,
    RunReconciler,
    SandboxCleanupController,
    SandboxReconciler,
)
from packages.application.tool_gateway import ExecutionTicketIssuer
from packages.infrastructure.database.public import (
    SqlAlchemyApprovalStore,
    SqlAlchemyRunStore,
    SqlAlchemySandboxLifecycleStore,
)
from packages.infrastructure.temporal import TemporalRunWorkflowControl


def build_database_platform_reconciler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    temporal_client: Client,
    sandbox_cleanup: SandboxCleanupController,
    ticket_issuer: ExecutionTicketIssuer | None,
    run_capacity_policy: RunCapacityPolicy | None = None,
    run_queue_batch_size: int = 50,
    run_queue_max_wait_seconds: int = 300,
    run_queue_max_pending_per_tenant: int = 1_000,
    run_capacity_domain_slots: Mapping[str, int] | None = None,
    run_capacity_lease_ttl_seconds: int = 300,
    run_capacity_tenant_quantum: int = 1,
) -> PlatformReconciler:
    """Compose DB stores and Temporal control while keeping Provider/Secret injection explicit."""

    workflow_control = TemporalRunWorkflowControl(temporal_client)
    return PlatformReconciler(
        runs=RunReconciler(
            SqlAlchemyRunStore(
                session_factory,
                run_capacity_policy=run_capacity_policy,
                queue_max_wait=timedelta(seconds=run_queue_max_wait_seconds),
                queue_max_pending_per_tenant=run_queue_max_pending_per_tenant,
                capacity_domain_slots=run_capacity_domain_slots,
                capacity_lease_ttl=timedelta(seconds=run_capacity_lease_ttl_seconds),
                capacity_tenant_quantum=run_capacity_tenant_quantum,
            ),
            workflow_control,
            batch_size=run_queue_batch_size,
        ),
        approvals=ApprovalReconciler(
            SqlAlchemyApprovalStore(session_factory),
            workflow_control,
            ticket_issuer,
        ),
        sandboxes=SandboxReconciler(
            SqlAlchemySandboxLifecycleStore(session_factory),
            sandbox_cleanup,
        ),
    )
