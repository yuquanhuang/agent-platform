"""Approval application authorization and decision tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.application.approvals import ApprovalManagementService, ApprovalStore
from packages.application.metadata import RequestMetadata
from packages.application.tool_gateway import ExecutionTicketIssue
from packages.contracts.generated.core_models import ApprovalDecisionRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
    dependency_unavailable,
)
from packages.contracts.temporal import ApprovalDecidedSignal
from packages.domain.public import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ExecutionTicketRecord,
    IdempotencyReplay,
    MutationOutcome,
    TenantAccess,
)
from packages.infrastructure.tool_gateway import HmacExecutionTicketIssuer

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUESTER_ID = UUID("22222222-2222-4222-8222-222222222222")
APPROVER_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
APPROVAL_ID = UUID("55555555-5555-4555-8555-555555555555")
DEPLOYMENT_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def record(**changes: object) -> ApprovalRequestRecord:
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
        status="PENDING",
        expires_at=NOW + timedelta(minutes=10),
        resource_version=1,
        self_approval_allowed=False,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(value, **changes)


class Stub:
    def __init__(self, actor_id: UUID = APPROVER_ID) -> None:
        self.actor_id = actor_id
        self.current = record()
        self.decisions: list[dict[str, object]] = []
        self.signals: list[object] = []
        self.signal_failures = 0
        self.ticket: ExecutionTicketRecord | None = None

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(self.actor_id),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset(
                {"approval:list", "approval:read", "approval:approve"}
            ),
        )

    async def list_approvals(self, context: TenantContext, **kwargs: object):
        return [self.current], None

    async def get_approval(self, context: TenantContext, **kwargs: object):
        return self.current

    async def decide(
        self, context: TenantContext, **kwargs: object
    ) -> MutationOutcome[ApprovalRequestRecord]:
        if self.current.status != "PENDING":
            return MutationOutcome[ApprovalRequestRecord](
                replay=IdempotencyReplay(
                    response_status=200,
                    response_body={
                        "id": str(self.current.id),
                        "run_id": str(self.current.run_id),
                        "tool_name": self.current.tool_name,
                        "parameter_digest": self.current.parameter_digest,
                        "status": self.current.status,
                        "expires_at": self.current.expires_at.isoformat(),
                        "resource_version": self.current.resource_version,
                    },
                    response_etag='"rv:2"',
                )
            )
        self.decisions.append(dict(kwargs))
        request = cast(ApprovalDecisionRequest, kwargs["request"])
        self.current = replace(
            self.current,
            status=request.decision,
            resource_version=2,
            updated_at=NOW + timedelta(seconds=1),
        )
        ticket_issue = kwargs.get("ticket_issue")
        if request.decision == "APPROVED" and ticket_issue is not None:
            credential = cast(ExecutionTicketIssue, ticket_issue).credential
            self.ticket = ExecutionTicketRecord(
                id=credential.ticket_id,
                tenant_id=TENANT_ID,
                approval_id=APPROVAL_ID,
                run_id=RUN_ID,
                execution_attempt=1,
                requester_id=REQUESTER_ID,
                tool_name="production.write",
                tool_schema_hash="sha256:" + "a" * 64,
                parameter_digest="sha256:" + "b" * 64,
                policy_version="sha256:" + "c" * 64,
                deployment_id=DEPLOYMENT_ID,
                nonce_hash=credential.nonce_hash,
                expires_at=self.current.expires_at,
                single_use=True,
                consumed_at=None,
                created_at=NOW + timedelta(seconds=1),
            )
        return MutationOutcome(value=self.current)

    async def get_ticket_for_approval(self, context: TenantContext, **kwargs: object):
        return self.ticket

    async def get_decision(self, context: TenantContext, **kwargs: object):
        request = cast(ApprovalDecisionRequest, self.decisions[-1]["request"])
        return ApprovalDecisionRecord(
            id=UUID("77777777-7777-4777-8777-777777777777"),
            tenant_id=TENANT_ID,
            approval_id=APPROVAL_ID,
            actor_id=self.actor_id,
            decision=request.decision,
            comment=request.comment,
            created_at=NOW + timedelta(seconds=1),
        )

    async def signal_approval(self, **kwargs: object) -> None:
        self.signals.append(kwargs["signal"])
        if self.signal_failures > 0:
            self.signal_failures -= 1
            raise dependency_unavailable("Temporal is temporarily unavailable.")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="approver",
        display_name="Approver",
        active_tenant_id=str(TENANT_ID),
        membership_version=1,
        auth_time=NOW,
    )


def metadata() -> RequestMetadata:
    return RequestMetadata(request_id="req-approval", trace_id="trace-approval")


@pytest.mark.asyncio
async def test_list_and_get_map_frozen_approval_shape() -> None:
    stub = Stub()
    service = ApprovalManagementService(
        stub, cast(ApprovalStore, stub), workflow_control=stub
    )

    page = await service.list_approvals(
        principal(),
        status="PENDING",
        run_id=str(RUN_ID),
        limit=20,
        cursor=None,
        metadata=metadata(),
    )
    detail = await service.get_approval(
        principal(), approval_id=str(APPROVAL_ID), metadata=metadata()
    )

    assert page.items == [detail]
    assert detail.tool_name == "production.write"
    assert detail.resource_version == 1


@pytest.mark.asyncio
async def test_decision_passes_if_match_and_idempotency_to_store() -> None:
    stub = Stub()
    service = ApprovalManagementService(
        stub, cast(ApprovalStore, stub), workflow_control=stub
    )

    result = await service.decide_approval(
        principal(),
        approval_id=str(APPROVAL_ID),
        request=ApprovalDecisionRequest(decision="APPROVED", comment="reviewed"),
        if_match='"rv:1"',
        idempotency_key="approval-key-001",
        metadata=metadata(),
    )

    assert result.status == "APPROVED"
    assert stub.decisions[0]["expected_version"] == 1
    assert stub.decisions[0]["actor_id"] == APPROVER_ID
    assert len(stub.signals) == 1


@pytest.mark.asyncio
async def test_approved_decision_issues_ticket_and_signals_only_safe_reference() -> (
    None
):
    stub = Stub()
    issuer = HmacExecutionTicketIssuer(SecretStr("s" * 32))
    service = ApprovalManagementService(
        stub,
        cast(ApprovalStore, stub),
        workflow_control=stub,
        execution_ticket_issuer=issuer,
    )

    await service.decide_approval(
        principal(),
        approval_id=str(APPROVAL_ID),
        request=ApprovalDecisionRequest(decision="APPROVED", comment="reviewed"),
        if_match='"rv:1"',
        idempotency_key="approval-key-ticket-001",
        metadata=metadata(),
    )

    assert stub.ticket is not None
    signal = cast(ApprovalDecidedSignal, stub.signals[0])
    assert signal.ticket_ref == f"execution-ticket:{stub.ticket.id}"
    assert signal.ticket_ref is not None
    assert stub.ticket.nonce_hash not in signal.ticket_ref
    assert (
        issuer.issue(
            tenant_id=TENANT_ID, approval_id=APPROVAL_ID
        ).nonce.get_secret_value()
        not in signal.ticket_ref
    )


@pytest.mark.asyncio
async def test_self_approval_is_denied_before_mutation() -> None:
    stub = Stub(actor_id=REQUESTER_ID)
    service = ApprovalManagementService(
        stub, cast(ApprovalStore, stub), workflow_control=stub
    )

    with pytest.raises(PlatformError) as raised:
        await service.decide_approval(
            principal(),
            approval_id=str(APPROVAL_ID),
            request=ApprovalDecisionRequest(decision="APPROVED", comment=None),
            if_match='"rv:1"',
            idempotency_key="approval-key-002",
            metadata=metadata(),
        )

    assert raised.value.status_code == 403
    assert stub.decisions == []


@pytest.mark.asyncio
async def test_expired_approval_is_rejected_before_mutation() -> None:
    stub = Stub()
    stub.current = record(status="EXPIRED", resource_version=2)
    service = ApprovalManagementService(
        stub, cast(ApprovalStore, stub), workflow_control=stub
    )

    with pytest.raises(PlatformError) as raised:
        await service.decide_approval(
            principal(),
            approval_id=str(APPROVAL_ID),
            request=ApprovalDecisionRequest(decision="APPROVED", comment=None),
            if_match='"rv:2"',
            idempotency_key="approval-key-003",
            metadata=metadata(),
        )

    assert raised.value.code == "RESOURCE_STATE_CONFLICT"
    assert stub.decisions == []


@pytest.mark.asyncio
async def test_idempotent_retry_redelivers_signal_after_durable_decision() -> None:
    stub = Stub()
    stub.signal_failures = 1
    service = ApprovalManagementService(
        stub, cast(ApprovalStore, stub), workflow_control=stub
    )
    request = ApprovalDecisionRequest(decision="REJECTED", comment="stop")

    with pytest.raises(PlatformError) as first_attempt:
        await service.decide_approval(
            principal(),
            approval_id=str(APPROVAL_ID),
            request=request,
            if_match='"rv:1"',
            idempotency_key="approval-key-signal-retry",
            metadata=metadata(),
        )

    assert first_attempt.value.code == "DEPENDENCY_UNAVAILABLE"
    assert stub.current.status == "REJECTED"
    retried = await service.decide_approval(
        principal(),
        approval_id=str(APPROVAL_ID),
        request=request,
        if_match='"rv:1"',
        idempotency_key="approval-key-signal-retry",
        metadata=metadata(),
    )

    assert retried.status == "REJECTED"
    assert len(stub.decisions) == 1
    assert len(stub.signals) == 2
    assert stub.signals[0] == stub.signals[1]
