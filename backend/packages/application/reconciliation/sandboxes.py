"""Truth-preserving cleanup of Sandbox instances leaked by completed Runs."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    SandboxServiceAccess,
)
from packages.contracts.public import PlatformError, TenantContext
from packages.contracts.sandbox_api import SandboxActionResponse

SandboxReconciliationReason = Literal[
    "TERMINAL_RUN",
    "EXPIRED_RUN_LEASE",
    "EXPIRED_SESSION_LEASE",
]
SandboxReconciliationAction = Literal["DESTROY", "MANUAL"]


@dataclass(frozen=True, slots=True)
class SandboxReconciliationCandidate:
    sandbox_id: UUID
    reason: SandboxReconciliationReason
    action: SandboxReconciliationAction


class SandboxReconciliationStore(Protocol):
    async def list_sandbox_reconciliation_candidates(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> Sequence[SandboxReconciliationCandidate]: ...


class SandboxCleanupController(Protocol):
    async def destroy(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
    ) -> SandboxActionResponse: ...


@dataclass(frozen=True, slots=True)
class SandboxReconciliationSummary:
    examined: int = 0
    destroyed: int = 0
    quarantined: int = 0
    cleanup_failed: int = 0
    unresolved: int = 0


class SandboxReconciler:
    """Use the lifecycle service so database state never outruns Provider truth."""

    def __init__(
        self,
        store: SandboxReconciliationStore,
        cleanup: SandboxCleanupController,
        *,
        batch_size: int = 50,
    ) -> None:
        if batch_size < 1:
            raise ValueError("Sandbox reconciliation batch_size must be positive")
        self._store = store
        self._cleanup = cleanup
        self._batch_size = batch_size

    async def reconcile_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> SandboxReconciliationSummary:
        candidates = await self._store.list_sandbox_reconciliation_candidates(
            context,
            now=now,
            limit=self._batch_size,
        )
        access = SandboxServiceAccess(
            context=context,
            permissions=frozenset({SANDBOX_MANAGE_PERMISSION}),
        )
        destroyed = quarantined = cleanup_failed = unresolved = 0
        for candidate in candidates:
            if candidate.action == "MANUAL":
                unresolved += 1
                continue
            try:
                result = await self._cleanup.destroy(
                    access,
                    sandbox_id=str(candidate.sandbox_id),
                )
            except PlatformError:
                # The lifecycle service records Provider uncertainty as QUARANTINED.
                cleanup_failed += 1
                continue
            if result.status == "TERMINATED":
                destroyed += 1
            elif result.status == "QUARANTINED":
                quarantined += 1
            else:
                unresolved += 1
        return SandboxReconciliationSummary(
            examined=len(candidates),
            destroyed=destroyed,
            quarantined=quarantined,
            cleanup_failed=cleanup_failed,
            unresolved=unresolved,
        )
