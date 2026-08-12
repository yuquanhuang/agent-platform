"""Idempotent Approval expiry, Ticket and Temporal Signal reconciliation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from packages.application.approvals import ApprovalWorkflowControl
from packages.application.tool_gateway import (
    ExecutionTicketIssue,
    ExecutionTicketIssuer,
    execution_ticket_ref,
)
from packages.contracts.public import TenantContext
from packages.contracts.temporal import ApprovalDecidedSignal
from packages.domain.public import ApprovalRequestRecord, ExecutionTicketRecord

ApprovalReconciliationDecision = Literal["APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]


@dataclass(frozen=True, slots=True)
class ApprovalSignalCandidate:
    approval: ApprovalRequestRecord
    decision_id: UUID | None
    decision: ApprovalReconciliationDecision
    decided_at: datetime
    ticket: ExecutionTicketRecord | None


class ApprovalReconciliationStore(Protocol):
    async def expire_due(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ApprovalRequestRecord, ...]: ...

    async def list_unsent_approval_signals(
        self,
        context: TenantContext,
        *,
        limit: int,
    ) -> tuple[ApprovalSignalCandidate, ...]: ...

    async def ensure_approved_ticket(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        ticket_issue: ExecutionTicketIssue,
        now: datetime,
    ) -> ExecutionTicketRecord | None: ...

    async def mark_workflow_signal_sent(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        sent_at: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApprovalReconciliationSummary:
    expired: int = 0
    tickets_repaired: int = 0
    signals_sent: int = 0
    unresolved: int = 0


class ApprovalReconciler:
    """Repair only delivery gaps derivable from immutable Approval facts."""

    def __init__(
        self,
        store: ApprovalReconciliationStore,
        workflow_control: ApprovalWorkflowControl,
        ticket_issuer: ExecutionTicketIssuer | None,
        *,
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError("Approval reconciliation batch_size must be positive")
        self._store = store
        self._workflow_control = workflow_control
        self._ticket_issuer = ticket_issuer
        self._batch_size = batch_size

    async def reconcile_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> ApprovalReconciliationSummary:
        expired = await self._store.expire_due(context, now=now, limit=self._batch_size)
        candidates = await self._store.list_unsent_approval_signals(
            context, limit=self._batch_size
        )
        tickets_repaired = signals_sent = unresolved = 0
        for candidate in candidates:
            decision_id = candidate.decision_id
            ticket = candidate.ticket
            if decision_id is None:
                unresolved += 1
                continue
            if candidate.decision == "APPROVED" and ticket is None:
                if self._ticket_issuer is None or candidate.approval.expires_at <= now:
                    unresolved += 1
                    continue
                ticket = await self._store.ensure_approved_ticket(
                    context,
                    approval_id=candidate.approval.id,
                    ticket_issue=ExecutionTicketIssue(
                        credential=self._ticket_issuer.issue(
                            tenant_id=candidate.approval.tenant_id,
                            approval_id=candidate.approval.id,
                        ),
                        expires_at=candidate.approval.expires_at,
                    ),
                    now=now,
                )
                if ticket is None:
                    unresolved += 1
                    continue
                tickets_repaired += 1
            signal = ApprovalDecidedSignal(
                signal_id=_signal_id(candidate.decision, decision_id),
                approval_id=candidate.approval.id,
                decision_id=decision_id,
                decision=candidate.decision,
                ticket_ref=(
                    execution_ticket_ref(ticket.id)
                    if candidate.decision == "APPROVED" and ticket is not None
                    else None
                ),
                decided_at=candidate.decided_at,
            )
            await self._workflow_control.signal_approval(
                tenant_id=candidate.approval.tenant_id,
                run_id=candidate.approval.run_id,
                signal=signal,
            )
            await self._store.mark_workflow_signal_sent(
                context,
                approval_id=candidate.approval.id,
                sent_at=now,
            )
            signals_sent += 1
        return ApprovalReconciliationSummary(
            expired=len(expired),
            tickets_repaired=tickets_repaired,
            signals_sent=signals_sent,
            unresolved=unresolved,
        )


def _signal_id(decision: ApprovalReconciliationDecision, decision_id: UUID) -> str:
    if decision == "EXPIRED":
        prefix = "approval-expiry"
    elif decision == "CANCELLED":
        prefix = "approval-cancellation"
    else:
        prefix = "approval-decision"
    return f"{prefix}:{decision_id}"
