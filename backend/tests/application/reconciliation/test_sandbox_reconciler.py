"""Sandbox reconciliation preserves Provider truth and manual boundaries."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.reconciliation import (
    SandboxCleanupController,
    SandboxReconciler,
    SandboxReconciliationCandidate,
    SandboxReconciliationStore,
)
from packages.application.sandbox import SandboxServiceAccess
from packages.contracts.public import SubjectType, TenantContext, dependency_unavailable
from packages.contracts.sandbox_api import SandboxActionResponse

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
TERMINAL_ID = UUID("11111111-1111-4111-8111-111111111111")
MANUAL_ID = UUID("22222222-2222-4222-8222-222222222222")
FAILED_ID = UUID("33333333-3333-4333-8333-333333333333")


def context() -> TenantContext:
    return TenantContext(
        tenant_id="44444444-4444-4444-8444-444444444444",
        subject_type=SubjectType.SERVICE,
        subject_id="55555555-5555-4555-8555-555555555555",
        auth_time=NOW,
        request_id="req-sandbox-reconcile",
        trace_id="trace-sandbox-reconcile",
    )


class Store:
    async def list_sandbox_reconciliation_candidates(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[SandboxReconciliationCandidate, ...]:
        del context, kwargs
        return (
            SandboxReconciliationCandidate(TERMINAL_ID, "TERMINAL_RUN", "DESTROY"),
            SandboxReconciliationCandidate(
                MANUAL_ID, "EXPIRED_SESSION_LEASE", "MANUAL"
            ),
            SandboxReconciliationCandidate(FAILED_ID, "EXPIRED_RUN_LEASE", "DESTROY"),
        )


class Cleanup:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def destroy(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse:
        access.authorize()
        resolved = UUID(sandbox_id)
        self.calls.append(resolved)
        if resolved == FAILED_ID:
            raise dependency_unavailable("Provider is unavailable.")
        return SandboxActionResponse(
            sandbox_id=sandbox_id,
            action="destroy",
            status="TERMINATED",
            changed=True,
        )


@pytest.mark.asyncio
async def test_reconciler_destroys_only_safe_candidates() -> None:
    cleanup = Cleanup()
    reconciler = SandboxReconciler(
        cast(SandboxReconciliationStore, Store()),
        cast(SandboxCleanupController, cleanup),
    )

    summary = await reconciler.reconcile_tenant_once(context(), now=NOW)

    assert summary.examined == 3
    assert summary.destroyed == 1
    assert summary.cleanup_failed == 1
    assert summary.unresolved == 1
    assert cleanup.calls == [TERMINAL_ID, FAILED_ID]
