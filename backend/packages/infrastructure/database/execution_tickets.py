"""Tenant-isolated Execution Ticket persistence and atomic consumption."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.metadata import RequestMetadata
from packages.application.tool_gateway import (
    ExecutionTicketIssue,
    ExecutionTicketStore,
    ToolExecutionDenied,
    ToolExecutionRequest,
    ToolExecutionResult,
    parse_execution_ticket_ref,
)
from packages.contracts.public import TenantContext, dependency_unavailable
from packages.domain.approvals import ApprovalRequestRecord
from packages.domain.execution_tickets import ExecutionTicketRecord
from packages.domain.runs import ensure_run_transition
from packages.infrastructure.database.models import (
    AgentRunModel,
    ApprovalRequestModel,
    AuditLogModel,
    DeploymentModel,
    ExecutionTicketModel,
    RoleBindingModel,
    RolePermissionModel,
    TenantMemberModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyExecutionTicketStore(ExecutionTicketStore):
    """Persist tickets and consume them before any external side effect."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_for_approval(
        self,
        context: TenantContext,
        *,
        approval: ApprovalRequestRecord,
        issue: ExecutionTicketIssue,
        now: datetime,
    ) -> ExecutionTicketRecord:
        if approval.status != "APPROVED":
            raise ToolExecutionDenied(
                "EXECUTION_TICKET_APPROVAL_NOT_APPROVED",
                "An Execution Ticket requires an approved Approval.",
            )
        model = ExecutionTicketModel(
            id=issue.credential.ticket_id,
            tenant_id=approval.tenant_id,
            approval_id=approval.id,
            run_id=approval.run_id,
            execution_attempt=approval.execution_attempt,
            requester_id=approval.requester_id,
            tool_name=approval.tool_name,
            tool_schema_hash=approval.tool_schema_hash,
            parameter_digest=approval.parameter_digest,
            policy_version=approval.policy_version,
            deployment_id=approval.deployment_id,
            nonce_hash=issue.credential.nonce_hash,
            expires_at=min(approval.expires_at, issue.expires_at),
            single_use=True,
            created_at=now,
        )
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                unit.session.add(model)
                await unit.session.flush()
                return _record(model)
        except IntegrityError:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                existing = await unit.session.scalar(
                    select(ExecutionTicketModel).where(
                        ExecutionTicketModel.tenant_id == approval.tenant_id,
                        ExecutionTicketModel.approval_id == approval.id,
                    )
                )
                if existing is not None and _same_ticket(existing, model):
                    return _record(existing)
            raise
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "Execution Ticket Store is unavailable."
            ) from error

    async def get_for_approval(
        self, context: TenantContext, *, approval_id: UUID
    ) -> ExecutionTicketRecord | None:
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                model = await unit.session.scalar(
                    select(ExecutionTicketModel).where(
                        ExecutionTicketModel.tenant_id == UUID(context.tenant_id),
                        ExecutionTicketModel.approval_id == approval_id,
                    )
                )
                return _record(model) if model is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "Execution Ticket Store is unavailable."
            ) from error

    async def consume(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ExecutionTicketRecord:
        ticket_id = parse_execution_ticket_ref(request.ticket_ref)
        nonce_hash = _sha256(request.ticket_nonce.get_secret_value())
        denial: str | None = None
        consumed: ExecutionTicketRecord | None = None
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                ticket = await session.scalar(
                    select(ExecutionTicketModel)
                    .where(
                        ExecutionTicketModel.tenant_id == request.tenant_id,
                        ExecutionTicketModel.id == ticket_id,
                    )
                    .with_for_update()
                )
                if ticket is None:
                    denial = "EXECUTION_TICKET_NOT_FOUND"
                    _audit(
                        session,
                        context,
                        request=request,
                        action="tool.denied",
                        result="DENIED",
                        reason_codes=[denial],
                        occurred_at=now,
                    )
                else:
                    approval = await session.scalar(
                        select(ApprovalRequestModel)
                        .where(
                            ApprovalRequestModel.tenant_id == request.tenant_id,
                            ApprovalRequestModel.id == request.approval_id,
                        )
                        .with_for_update()
                    )
                    run = await session.scalar(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.tenant_id == request.tenant_id,
                            AgentRunModel.id == request.run_id,
                        )
                        .with_for_update()
                    )
                    denial = _ticket_denial(
                        ticket, approval, run, request, nonce_hash, now
                    )
                    if denial is not None:
                        if (
                            denial == "EXECUTION_TICKET_EXPIRED"
                            and approval is not None
                            and approval.status == "APPROVED"
                        ):
                            approval.status = "EXPIRED"
                            approval.resource_version += 1
                            approval.updated_at = now
                            if run is not None and run.status == "WAITING_APPROVAL":
                                ensure_run_transition("WAITING_APPROVAL", "TIMEOUT")
                                run.status = "TIMEOUT"
                                run.error_code = "EXECUTION_TICKET_EXPIRED"
                                run.finished_at = now
                        _audit(
                            session,
                            context,
                            request=request,
                            action=(
                                "ticket.replay" if ticket.consumed_at else "tool.denied"
                            ),
                            result="DENIED",
                            reason_codes=[denial],
                            occurred_at=now,
                        )
                    else:
                        if approval is None or run is None:
                            raise RuntimeError(
                                "Validated Execution Ticket bindings are unavailable"
                            )
                        ticket.consumed_at = now
                        approval.status = "CONSUMED"
                        approval.resource_version += 1
                        approval.updated_at = now
                        if run.status == "WAITING_APPROVAL":
                            ensure_run_transition("WAITING_APPROVAL", "RUNNING")
                            run.status = "RUNNING"
                        await session.flush()
                        _audit(
                            session,
                            context,
                            request=request,
                            action="ticket.consume",
                            result="SUCCESS",
                            reason_codes=[],
                            occurred_at=now,
                        )
                        consumed = _record(ticket)
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "Execution Ticket Store is unavailable."
            ) from error
        if denial is not None:
            raise ToolExecutionDenied(denial, "The Execution Ticket is not valid.")
        if consumed is None:
            raise RuntimeError("Execution Ticket consumption produced no result")
        return consumed

    async def audit_tool_result(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        result: ToolExecutionResult,
        now: datetime,
    ) -> None:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                _audit(
                    unit.session,
                    context,
                    request=request,
                    action="tool.execute",
                    result="SUCCESS" if result.status == "SUCCEEDED" else "FAILED",
                    reason_codes=([result.error_code] if result.error_code else []),
                    occurred_at=now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Execution audit is unavailable.") from error

    async def audit_denied(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        reason_codes: tuple[str, ...],
        now: datetime,
    ) -> None:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                _audit(
                    unit.session,
                    context,
                    request=request,
                    action="tool.denied",
                    result="DENIED",
                    reason_codes=list(reason_codes),
                    occurred_at=now,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Execution audit is unavailable.") from error


def _ticket_denial(
    ticket: ExecutionTicketModel,
    approval: ApprovalRequestModel | None,
    run: AgentRunModel | None,
    request: ToolExecutionRequest,
    nonce_hash: str,
    now: datetime,
) -> str | None:
    if not hmac.compare_digest(ticket.nonce_hash, nonce_hash):
        return "EXECUTION_TICKET_NONCE_MISMATCH"
    if ticket.consumed_at is not None:
        return "EXECUTION_TICKET_REPLAY"
    if ticket.expires_at <= now:
        return "EXECUTION_TICKET_EXPIRED"
    if approval is None or approval.status != "APPROVED":
        return "EXECUTION_TICKET_APPROVAL_NOT_APPROVED"
    if run is None:
        return "EXECUTION_TICKET_RUN_NOT_FOUND"
    pairs = (
        (ticket.approval_id, request.approval_id, "EXECUTION_TICKET_APPROVAL_MISMATCH"),
        (ticket.run_id, request.run_id, "EXECUTION_TICKET_RUN_MISMATCH"),
        (
            ticket.execution_attempt,
            request.execution_attempt,
            "EXECUTION_TICKET_ATTEMPT_MISMATCH",
        ),
        (
            ticket.requester_id,
            request.requester_id,
            "EXECUTION_TICKET_REQUESTER_MISMATCH",
        ),
        (
            ticket.deployment_id,
            request.deployment_id,
            "EXECUTION_TICKET_DEPLOYMENT_MISMATCH",
        ),
        (ticket.tool_name, request.tool_name, "EXECUTION_TICKET_TOOL_MISMATCH"),
        (
            ticket.tool_schema_hash,
            request.tool_schema_hash,
            "EXECUTION_TICKET_SCHEMA_MISMATCH",
        ),
        (
            ticket.parameter_digest,
            request.parameter_digest,
            "EXECUTION_TICKET_PARAMETER_MISMATCH",
        ),
        (
            ticket.policy_version,
            request.policy_version,
            "EXECUTION_TICKET_POLICY_MISMATCH",
        ),
    )
    for expected, actual, reason in pairs:
        if expected != actual:
            return reason
    if run.current_attempt != request.execution_attempt:
        return "EXECUTION_TICKET_STALE_ATTEMPT"
    if run.status not in {"WAITING_APPROVAL", "RUNNING"}:
        return "EXECUTION_TICKET_RUN_NOT_EXECUTABLE"
    if (
        approval.run_id != request.run_id
        or approval.deployment_id != request.deployment_id
    ):
        return "EXECUTION_TICKET_APPROVAL_BINDING_CHANGED"
    return None


def _same_ticket(left: ExecutionTicketModel, right: ExecutionTicketModel) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "id",
            "tenant_id",
            "approval_id",
            "run_id",
            "execution_attempt",
            "requester_id",
            "tool_name",
            "tool_schema_hash",
            "parameter_digest",
            "policy_version",
            "deployment_id",
            "nonce_hash",
            "expires_at",
            "single_use",
        )
    )


def _record(model: ExecutionTicketModel) -> ExecutionTicketRecord:
    return ExecutionTicketRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        approval_id=model.approval_id,
        run_id=model.run_id,
        execution_attempt=model.execution_attempt,
        requester_id=model.requester_id,
        tool_name=model.tool_name,
        tool_schema_hash=model.tool_schema_hash,
        parameter_digest=model.parameter_digest,
        policy_version=model.policy_version,
        deployment_id=model.deployment_id,
        nonce_hash=model.nonce_hash,
        expires_at=model.expires_at,
        single_use=model.single_use,
        consumed_at=model.consumed_at,
        created_at=model.created_at,
    )


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _audit(
    session: AsyncSession,
    context: TenantContext,
    *,
    request: ToolExecutionRequest,
    action: str,
    result: str,
    reason_codes: list[str],
    occurred_at: datetime,
) -> None:
    metadata = {
        "approval_id": str(request.approval_id),
        "run_id": str(request.run_id),
        "execution_attempt": request.execution_attempt,
        "tool_name": request.tool_name,
        "tool_schema_hash": request.tool_schema_hash,
        "parameter_digest": request.parameter_digest,
        "policy_version": request.policy_version,
        "deployment_id": str(request.deployment_id),
    }
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    from uuid import NAMESPACE_URL, uuid5

    session.add(
        AuditLogModel(
            id=uuid5(
                NAMESPACE_URL,
                f"audit/{context.trace_id}/{action}/{request.approval_id}/{occurred_at.isoformat()}",
            ),
            tenant_id=request.tenant_id,
            actor_type=context.subject_type.value,
            actor_id=UUID(context.subject_id),
            action=action,
            resource_type="execution_ticket" if action.startswith("ticket") else "tool",
            resource_id=request.approval_id,
            result=result,
            reason_codes=reason_codes,
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_schema_version=1,
            metadata_json=metadata,
            created_at=occurred_at,
        )
    )


class SqlAlchemyToolAuthorizationResolver:
    """Re-check active Deployment and current tenant execute permission."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, context: TenantContext, *, request: ToolExecutionRequest):
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                session = unit.session
                member = await session.scalar(
                    select(TenantMemberModel).where(
                        TenantMemberModel.tenant_id == request.tenant_id,
                        TenantMemberModel.user_id == request.requester_id,
                        TenantMemberModel.status == "ACTIVE",
                    )
                )
                deployment = await session.scalar(
                    select(DeploymentModel).where(
                        DeploymentModel.tenant_id == request.tenant_id,
                        DeploymentModel.id == request.deployment_id,
                        DeploymentModel.status.in_(("ACTIVE", "DEGRADED")),
                    )
                )
                permission = await session.scalar(
                    select(RolePermissionModel)
                    .join(
                        RoleBindingModel,
                        (RoleBindingModel.tenant_id == RolePermissionModel.tenant_id)
                        & (RoleBindingModel.role_id == RolePermissionModel.role_id),
                    )
                    .where(
                        RolePermissionModel.tenant_id == request.tenant_id,
                        RolePermissionModel.resource_type == "mcp",
                        RolePermissionModel.action == "execute",
                        RoleBindingModel.tenant_id == request.tenant_id,
                        RoleBindingModel.subject_type == "user",
                        RoleBindingModel.subject_id == request.requester_id,
                    )
                )
                return _authorization_context(
                    request,
                    permission_allowed=member is not None and permission is not None,
                    policy_allowed=deployment is not None,
                    reason_codes=(("TENANT_MEMBER_INACTIVE",) if member is None else ())
                    + (("DEPLOYMENT_NOT_ACTIVE",) if deployment is None else ()),
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable(
                "Tool Authorization is unavailable."
            ) from error


def _authorization_context(
    request: ToolExecutionRequest,
    *,
    permission_allowed: bool,
    policy_allowed: bool,
    reason_codes: tuple[str, ...],
):
    from packages.application.tool_gateway import ToolAuthorizationContext

    return ToolAuthorizationContext(
        permission_allowed=permission_allowed,
        policy_allowed=policy_allowed,
        tool_name=request.tool_name,
        tool_schema_hash=request.tool_schema_hash,
        policy_version=request.policy_version,
        deployment_id=request.deployment_id,
        reason_codes=reason_codes,
    )
