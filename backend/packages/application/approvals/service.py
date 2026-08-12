"""Approval request creation, query, expiry and decision use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.application.tool_gateway import (
    ExecutionTicketIssue,
    ExecutionTicketIssuer,
    execution_ticket_ref,
)
from packages.contracts.generated.core_models import (
    Approval,
    ApprovalDecisionRequest,
    ApprovalPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.contracts.temporal import ApprovalDecidedSignal
from packages.domain.public import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ExecutionTicketRecord,
    MutationOutcome,
    TenantAccess,
    decode_cursor,
    format_etag,
    parse_etag,
)


@dataclass(frozen=True, slots=True)
class ApprovalRequestInput:
    """Trusted policy output used to park one runtime tool invocation."""

    run_id: UUID
    execution_attempt: int
    requester_id: UUID
    tool_call_id: str
    tool_name: str
    tool_schema_hash: str
    parameter_digest: str
    policy_version: str
    deployment_id: UUID
    expires_at: datetime
    self_approval_allowed: bool = False
    runtime_checkpoint_ref: str | None = None


class ApprovalAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class ApprovalStore(Protocol):
    async def create_request(
        self,
        context: TenantContext,
        *,
        request: ApprovalRequestInput,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ApprovalRequestRecord: ...

    async def list_approvals(
        self,
        context: TenantContext,
        *,
        status: str | None,
        run_id: UUID | None,
        limit: int,
        cursor: str | None,
        now: datetime,
    ) -> tuple[list[ApprovalRequestRecord], str | None]: ...

    async def get_approval(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        now: datetime,
    ) -> ApprovalRequestRecord | None: ...

    async def get_decision(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
    ) -> ApprovalDecisionRecord | None: ...

    async def decide(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        actor_id: UUID,
        request: ApprovalDecisionRequest,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        now: datetime,
        ticket_issue: ExecutionTicketIssue | None = None,
    ) -> MutationOutcome[ApprovalRequestRecord] | None: ...

    async def get_ticket_for_approval(
        self, context: TenantContext, *, approval_id: UUID
    ) -> ExecutionTicketRecord | None: ...

    async def expire_due(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ApprovalRequestRecord, ...]: ...


class ApprovalWorkflowControl(Protocol):
    async def signal_approval(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        signal: ApprovalDecidedSignal,
    ) -> None: ...


class ApprovalSignalDeliveryStore(Protocol):
    async def mark_workflow_signal_sent(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        sent_at: datetime,
    ) -> bool: ...


class ApprovalManagementService:
    """Map the frozen Approval API to tenant-scoped durable facts."""

    def __init__(
        self,
        access_resolver: ApprovalAccessResolver,
        store: ApprovalStore,
        workflow_control: ApprovalWorkflowControl | None = None,
        execution_ticket_issuer: ExecutionTicketIssuer | None = None,
        signal_delivery_store: ApprovalSignalDeliveryStore | None = None,
    ):
        self._access_resolver = access_resolver
        self._store = store
        self._workflow_control = workflow_control
        self._execution_ticket_issuer = execution_ticket_issuer
        self._signal_delivery_store = signal_delivery_store

    async def list_approvals(
        self,
        principal: AuthenticatedPrincipal,
        *,
        status: str | None,
        run_id: str | None,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> ApprovalPage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._store.list_approvals(
            access.context,
            status=status,
            run_id=_optional_uuid(run_id, "run_id"),
            limit=limit,
            cursor=cursor,
            now=datetime.now(UTC),
        )
        return ApprovalPage(
            items=[_approval(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def get_approval(
        self,
        principal: AuthenticatedPrincipal,
        *,
        approval_id: str,
        metadata: RequestMetadata,
    ) -> Approval:
        access = await self._access(principal, "read", metadata)
        record = await self._store.get_approval(
            access.context,
            approval_id=_uuid(approval_id, "approval_id"),
            now=datetime.now(UTC),
        )
        if record is None:
            raise resource_not_found()
        return _approval(record)

    async def decide_approval(
        self,
        principal: AuthenticatedPrincipal,
        *,
        approval_id: str,
        request: ApprovalDecisionRequest,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> Approval:
        access = await self._access(principal, "approve", metadata)
        parsed_id = _uuid(approval_id, "approval_id")
        expected_version = _expected_version(if_match)
        current = await self._store.get_approval(
            access.context,
            approval_id=parsed_id,
            now=datetime.now(UTC),
        )
        if current is None:
            raise resource_not_found()
        actor_id = _uuid(access.context.subject_id, "subject_id")
        if not current.self_approval_allowed and current.requester_id == actor_id:
            raise permission_denied()
        if current.status == "EXPIRED":
            raise resource_state_conflict("The Approval has expired.")
        request_hash = canonical_request_hash(
            "approval.decision",
            request,
            extra={"approval_id": approval_id, "if_match": if_match},
        )
        decision_time = datetime.now(UTC)
        if request.decision == "APPROVED" and self._execution_ticket_issuer is not None:
            outcome = await self._store.decide(
                access.context,
                approval_id=parsed_id,
                actor_id=actor_id,
                request=request,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                metadata=metadata,
                now=decision_time,
                ticket_issue=ExecutionTicketIssue(
                    credential=self._execution_ticket_issuer.issue(
                        tenant_id=current.tenant_id,
                        approval_id=parsed_id,
                    ),
                    expires_at=current.expires_at,
                ),
            )
        else:
            outcome = await self._store.decide(
                access.context,
                approval_id=parsed_id,
                actor_id=actor_id,
                request=request,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                metadata=metadata,
                now=decision_time,
            )
        if outcome is None:
            raise resource_not_found()
        approval = (
            Approval.model_validate(outcome.replay.response_body)
            if outcome.replay is not None
            else _approval(_required_value(outcome))
        )
        decision = await self._store.get_decision(access.context, approval_id=parsed_id)
        if decision is None:
            raise RuntimeError("Approval decision fact is unavailable")
        if self._workflow_control is None:
            from packages.contracts.public import dependency_unavailable

            raise dependency_unavailable("Approval Workflow control is not configured.")
        ticket = None
        if request.decision == "APPROVED" and self._execution_ticket_issuer is not None:
            ticket = await self._store.get_ticket_for_approval(
                access.context, approval_id=parsed_id
            )
        await self._workflow_control.signal_approval(
            tenant_id=current.tenant_id,
            run_id=current.run_id,
            signal=ApprovalDecidedSignal(
                signal_id=f"approval-decision:{decision.id}",
                approval_id=decision.approval_id,
                decision_id=decision.id,
                decision=decision.decision,
                ticket_ref=(
                    execution_ticket_ref(ticket.id)
                    if request.decision == "APPROVED" and ticket is not None
                    else None
                ),
                decided_at=decision.created_at,
            ),
        )
        if self._signal_delivery_store is not None:
            await self._signal_delivery_store.mark_workflow_signal_sent(
                access.context,
                approval_id=parsed_id,
                sent_at=datetime.now(UTC),
            )
        return approval

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("approval", action):
            raise permission_denied()
        return access


class ApprovalCoordinator:
    """Internal runtime boundary for creating and expiring approval facts."""

    def __init__(
        self,
        store: ApprovalStore,
        workflow_control: ApprovalWorkflowControl | None = None,
        signal_delivery_store: ApprovalSignalDeliveryStore | None = None,
    ) -> None:
        self._store = store
        self._workflow_control = workflow_control
        self._signal_delivery_store = signal_delivery_store

    async def request_approval(
        self,
        context: TenantContext,
        *,
        request: ApprovalRequestInput,
        metadata: RequestMetadata,
        now: datetime | None = None,
    ) -> ApprovalRequestRecord:
        resolved_now = now or datetime.now(UTC)
        if request.execution_attempt < 1:
            raise validation_error("execution_attempt must be positive.")
        if request.expires_at <= resolved_now:
            raise validation_error("Approval expires_at must be in the future.")
        return await self._store.create_request(
            context,
            request=request,
            metadata=metadata,
            now=resolved_now,
        )

    async def expire_due(
        self,
        context: TenantContext,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> tuple[ApprovalRequestRecord, ...]:
        if not 1 <= limit <= 1000:
            raise validation_error("limit must be between 1 and 1000.")
        expired = await self._store.expire_due(
            context, now=now or datetime.now(UTC), limit=limit
        )
        if expired and self._workflow_control is None:
            from packages.contracts.public import dependency_unavailable

            raise dependency_unavailable("Approval Workflow control is not configured.")
        for record in expired:
            decided_at = now or datetime.now(UTC)
            decision_id = uuid5(
                NAMESPACE_URL,
                f"approval-expiry/{record.tenant_id}/{record.id}/"
                f"{record.resource_version}",
            )
            await self._workflow_control.signal_approval(  # type: ignore[union-attr]
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                signal=ApprovalDecidedSignal(
                    signal_id=f"approval-expiry:{decision_id}",
                    approval_id=record.id,
                    decision_id=decision_id,
                    decision="EXPIRED",
                    ticket_ref=None,
                    decided_at=decided_at,
                ),
            )
            if self._signal_delivery_store is not None:
                await self._signal_delivery_store.mark_workflow_signal_sent(
                    context,
                    approval_id=record.id,
                    sent_at=decided_at,
                )
        return expired


def _approval(record: ApprovalRequestRecord) -> Approval:
    return Approval(
        id=str(record.id),
        run_id=str(record.run_id),
        tool_name=record.tool_name,
        parameter_digest=record.parameter_digest,
        status=record.status,
        expires_at=record.expires_at,
        resource_version=record.resource_version,
    )


def _required_value(
    outcome: MutationOutcome[ApprovalRequestRecord],
) -> ApprovalRequestRecord:
    if outcome.value is None:
        raise RuntimeError("Approval decision outcome is missing its Approval")
    return outcome.value


def approval_etag(approval: Approval) -> str:
    return format_etag(approval.resource_version)


def validate_cursor(cursor: str) -> None:
    try:
        decode_cursor(cursor)
    except ValueError as error:
        raise validation_error("Pagination cursor is invalid.") from error


def _expected_version(if_match: str) -> int:
    try:
        return parse_etag(if_match)
    except ValueError as error:
        raise resource_version_conflict() from error


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise validation_error(f"{field_name} must be a UUID.") from error


def _optional_uuid(value: str | None, field_name: str) -> UUID | None:
    return _uuid(value, field_name) if value is not None else None
