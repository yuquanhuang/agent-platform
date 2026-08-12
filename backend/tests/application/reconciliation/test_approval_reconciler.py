"""Approval expiry, Ticket reconstruction and Signal delivery compensation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.application.approvals import ApprovalWorkflowControl
from packages.application.reconciliation import (
    ApprovalReconciler,
    ApprovalReconciliationStore,
    ApprovalSignalCandidate,
)
from packages.application.tool_gateway import ExecutionTicketIssue
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import ApprovalDecidedSignal
from packages.domain.public import ApprovalRequestRecord, ExecutionTicketRecord
from packages.infrastructure.tool_gateway import HmacExecutionTicketIssuer

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
REQUESTER_ID = UUID("33333333-3333-4333-8333-333333333333")
DEPLOYMENT_ID = UUID("44444444-4444-4444-8444-444444444444")
APPROVAL_ID = UUID("55555555-5555-4555-8555-555555555555")
DECISION_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id="77777777-7777-4777-8777-777777777777",
        auth_time=NOW,
        request_id="req-approval-reconcile",
        trace_id="trace-approval-reconcile",
    )


def approval(**changes: object) -> ApprovalRequestRecord:
    value = ApprovalRequestRecord(
        id=APPROVAL_ID,
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        requester_id=REQUESTER_ID,
        tool_call_id="tool-call-1",
        tool_name="production.write",
        tool_schema_hash="sha256:" + "a" * 64,
        parameter_digest="sha256:" + "b" * 64,
        policy_version="sha256:" + "c" * 64,
        deployment_id=DEPLOYMENT_ID,
        status="APPROVED",
        expires_at=NOW + timedelta(minutes=10),
        resource_version=2,
        self_approval_allowed=False,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(seconds=30),
    )
    return replace(value, **changes)


class Store:
    def __init__(self) -> None:
        self.candidates = (
            ApprovalSignalCandidate(
                approval=approval(),
                decision_id=DECISION_ID,
                decision="APPROVED",
                decided_at=NOW - timedelta(seconds=30),
                ticket=None,
            ),
            ApprovalSignalCandidate(
                approval=approval(id=UUID("88888888-8888-4888-8888-888888888888")),
                decision_id=None,
                decision="REJECTED",
                decided_at=NOW - timedelta(seconds=20),
                ticket=None,
            ),
        )
        self.marked: list[UUID] = []
        self.tickets: list[ExecutionTicketRecord] = []

    async def expire_due(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return (approval(status="EXPIRED", resource_version=3),)

    async def list_unsent_approval_signals(
        self, context: TenantContext, **kwargs: object
    ) -> tuple[ApprovalSignalCandidate, ...]:
        del context, kwargs
        return self.candidates

    async def ensure_approved_ticket(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        ticket_issue: ExecutionTicketIssue,
        now: datetime,
    ) -> ExecutionTicketRecord:
        del context
        ticket = ExecutionTicketRecord(
            id=ticket_issue.credential.ticket_id,
            tenant_id=TENANT_ID,
            approval_id=approval_id,
            run_id=RUN_ID,
            execution_attempt=1,
            requester_id=REQUESTER_ID,
            tool_name="production.write",
            tool_schema_hash="sha256:" + "a" * 64,
            parameter_digest="sha256:" + "b" * 64,
            policy_version="sha256:" + "c" * 64,
            deployment_id=DEPLOYMENT_ID,
            nonce_hash=ticket_issue.credential.nonce_hash,
            expires_at=ticket_issue.expires_at,
            single_use=True,
            consumed_at=None,
            created_at=now,
        )
        self.tickets.append(ticket)
        return ticket

    async def mark_workflow_signal_sent(
        self, context: TenantContext, *, approval_id: UUID, sent_at: datetime
    ) -> bool:
        del context, sent_at
        self.marked.append(approval_id)
        return True


class Workflow:
    def __init__(self) -> None:
        self.signals: list[ApprovalDecidedSignal] = []

    async def signal_approval(self, **kwargs: object) -> None:
        self.signals.append(cast(ApprovalDecidedSignal, kwargs["signal"]))


@pytest.mark.asyncio
async def test_reconciler_repairs_ticket_and_marks_only_delivered_signal() -> None:
    store = Store()
    workflow = Workflow()
    reconciler = ApprovalReconciler(
        cast(ApprovalReconciliationStore, store),
        cast(ApprovalWorkflowControl, workflow),
        HmacExecutionTicketIssuer(SecretStr("s" * 32)),
    )

    summary = await reconciler.reconcile_tenant_once(context(), now=NOW)

    assert summary.expired == 1
    assert summary.tickets_repaired == 1
    assert summary.signals_sent == 1
    assert summary.unresolved == 1
    assert store.marked == [APPROVAL_ID]
    assert workflow.signals[0].signal_id == f"approval-decision:{DECISION_ID}"
    assert workflow.signals[0].ticket_ref == f"execution-ticket:{store.tickets[0].id}"


@pytest.mark.asyncio
async def test_reconciler_does_not_forge_ticket_without_issuer() -> None:
    store = Store()
    workflow = Workflow()
    reconciler = ApprovalReconciler(
        cast(ApprovalReconciliationStore, store),
        cast(ApprovalWorkflowControl, workflow),
        None,
    )

    summary = await reconciler.reconcile_tenant_once(context(), now=NOW)

    assert summary.tickets_repaired == 0
    assert summary.signals_sent == 0
    assert summary.unresolved == 2
    assert workflow.signals == []
    assert store.marked == []
