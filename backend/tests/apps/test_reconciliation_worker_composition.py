"""Reconciliation worker production composition tests."""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from apps.reconciliation_worker.composition import (
    build_database_platform_reconciler,
)
from packages.application.reconciliation import (
    PlatformReconciler,
    SandboxCleanupController,
)


def test_composition_requires_explicit_temporal_and_sandbox_dependencies() -> None:
    reconciler = build_database_platform_reconciler(
        cast(async_sessionmaker[AsyncSession], object()),
        temporal_client=cast(Client, object()),
        sandbox_cleanup=cast(SandboxCleanupController, object()),
        ticket_issuer=None,
    )

    assert isinstance(reconciler, PlatformReconciler)
