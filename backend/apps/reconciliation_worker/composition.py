"""Explicit production composition for the platform reconciliation worker."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

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
) -> PlatformReconciler:
    """Compose DB stores and Temporal control while keeping Provider/Secret injection explicit."""

    workflow_control = TemporalRunWorkflowControl(temporal_client)
    return PlatformReconciler(
        runs=RunReconciler(
            SqlAlchemyRunStore(session_factory),
            workflow_control,
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
