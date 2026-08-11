"""PostgreSQL Approval persistence with tenant isolation and immutable facts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.approvals import ApprovalRequestInput, ApprovalStore
from packages.application.metadata import RequestMetadata
from packages.application.tool_gateway import ExecutionTicketIssue
from packages.contracts.generated.core_models import ApprovalDecisionRequest
from packages.contracts.generated.run_event import RUNTIME_EVENT_CANDIDATE_ADAPTER
from packages.contracts.public import (
    TenantContext,
    dependency_unavailable,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    ApprovalDecision,
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ApprovalStatus,
    ExecutionTicketRecord,
    MutationOutcome,
    decode_cursor,
    encode_cursor,
    ensure_run_transition,
)
from packages.infrastructure.database.events import append_control_run_event
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentRunModel,
    ApprovalDecisionModel,
    ApprovalRequestModel,
    AuditLogModel,
    ExecutionTicketModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

_EXPIRY_ACTOR_ID = UUID(int=0)


class SqlAlchemyApprovalStore(ApprovalStore):
    """Persist ApprovalRequest and append-only Decision facts atomically."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_request(
        self,
        context: TenantContext,
        *,
        request: ApprovalRequestInput,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ApprovalRequestRecord:
        tenant_id = UUID(context.tenant_id)
        approval_id = uuid5(
            NAMESPACE_URL,
            f"approval/{tenant_id}/{request.run_id}/{request.execution_attempt}/"
            f"{request.tool_call_id}",
        )
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                run = await session.scalar(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == request.run_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise resource_state_conflict("The Run is unavailable.")
                existing = await session.scalar(
                    select(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.tenant_id == tenant_id,
                        ApprovalRequestModel.id == approval_id,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if not _same_request(existing, request):
                        raise resource_state_conflict(
                            "The Approval request identity was reused with different facts."
                        )
                    return _approval_record(existing)
                if run.current_attempt != request.execution_attempt:
                    raise resource_state_conflict(
                        "The Approval execution attempt is stale."
                    )
                if run.status not in {"RUNNING", "WAITING_APPROVAL"}:
                    raise resource_state_conflict(
                        "The Run cannot accept an Approval request in its current state."
                    )
                if request.expires_at <= now:
                    raise validation_error("Approval expires_at must be in the future.")
                model = ApprovalRequestModel(
                    id=approval_id,
                    tenant_id=tenant_id,
                    run_id=request.run_id,
                    execution_attempt=request.execution_attempt,
                    requester_id=request.requester_id,
                    tool_call_id=request.tool_call_id,
                    tool_name=request.tool_name,
                    tool_schema_hash=request.tool_schema_hash,
                    parameter_digest=request.parameter_digest,
                    policy_version=request.policy_version,
                    deployment_id=request.deployment_id,
                    status="PENDING",
                    expires_at=request.expires_at,
                    resource_version=1,
                    self_approval_allowed=request.self_approval_allowed,
                    created_at=now,
                    updated_at=now,
                )
                session.add(model)
                await session.flush()
                if run.status == "RUNNING":
                    ensure_run_transition("RUNNING", "WAITING_APPROVAL")
                    run.status = "WAITING_APPROVAL"
                candidate = RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                    {
                        "source_event_id": f"approval-required:{approval_id}",
                        "event_type": "approval_required",
                        "occurred_at": now,
                        "payload_version": "1.0",
                        "payload": {
                            "approval_id": str(approval_id),
                            "tool_name": request.tool_name,
                            "parameter_digest": request.parameter_digest,
                            "expires_at": request.expires_at.isoformat(),
                        },
                    }
                )
                await append_control_run_event(
                    session,
                    context,
                    run=run,
                    execution_attempt=request.execution_attempt,
                    candidate=candidate,
                )
                _audit(
                    session,
                    context,
                    action="approval.request",
                    approval_id=approval_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={
                        "approval_id": str(approval_id),
                        "tool_name": request.tool_name,
                        "parameter_digest": request.parameter_digest,
                        "expires_at": request.expires_at.isoformat(),
                    },
                    occurred_at=now,
                )
                return _approval_record(model)
        except IntegrityError as error:
            try:
                async with TenantUnitOfWork(
                    self._session_factory, context, read_only=True
                ) as unit:
                    existing = await unit.session.scalar(
                        select(ApprovalRequestModel).where(
                            ApprovalRequestModel.tenant_id == tenant_id,
                            ApprovalRequestModel.id == approval_id,
                        )
                    )
            except SQLAlchemyError as lookup_error:
                raise dependency_unavailable(
                    "Approval Store is unavailable."
                ) from lookup_error
            if existing is not None and _same_request(existing, request):
                return _approval_record(existing)
            raise dependency_unavailable("Approval Store is unavailable.") from error
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

    async def list_approvals(
        self,
        context: TenantContext,
        *,
        status: str | None,
        run_id: UUID | None,
        limit: int,
        cursor: str | None,
        now: datetime,
    ) -> tuple[list[ApprovalRequestRecord], str | None]:
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200.")
        if status is not None and status not in {
            "PENDING",
            "APPROVED",
            "REJECTED",
            "EXPIRED",
            "CANCELLED",
            "CONSUMED",
        }:
            raise validation_error("Approval status is invalid.")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                await _expire_due_in_transaction(session, context, now=now, limit=1000)
                statement = select(ApprovalRequestModel).where(
                    ApprovalRequestModel.tenant_id == tenant_id
                )
                if status is not None:
                    statement = statement.where(ApprovalRequestModel.status == status)
                if run_id is not None:
                    statement = statement.where(ApprovalRequestModel.run_id == run_id)
                statement = statement.order_by(
                    ApprovalRequestModel.created_at.desc(),
                    ApprovalRequestModel.id.desc(),
                )
                if cursor is not None:
                    try:
                        created_at, approval_id = decode_cursor(cursor)
                    except ValueError as error:
                        raise validation_error(
                            "Pagination cursor is invalid."
                        ) from error
                    statement = statement.where(
                        or_(
                            ApprovalRequestModel.created_at < created_at,
                            and_(
                                ApprovalRequestModel.created_at == created_at,
                                ApprovalRequestModel.id < approval_id,
                            ),
                        )
                    )
                rows = list((await session.scalars(statement.limit(limit + 1))).all())
                page = rows[:limit]
                next_cursor = (
                    encode_cursor(page[-1].created_at, page[-1].id)
                    if len(rows) > limit and page
                    else None
                )
                return [_approval_record(row) for row in page], next_cursor
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

    async def get_approval(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        now: datetime,
    ) -> ApprovalRequestRecord | None:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                row = await session.scalar(
                    select(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.tenant_id == UUID(context.tenant_id),
                        ApprovalRequestModel.id == approval_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.status == "PENDING" and row.expires_at <= now:
                    await _expire_one(session, context, row=row, now=now)
                return _approval_record(row)
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

    async def get_decision(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
    ) -> ApprovalDecisionRecord | None:
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                row = await unit.session.scalar(
                    select(ApprovalDecisionModel).where(
                        ApprovalDecisionModel.tenant_id == UUID(context.tenant_id),
                        ApprovalDecisionModel.approval_id == approval_id,
                    )
                )
                return _decision_record(row) if row is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

    async def get_ticket_for_approval(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
    ) -> ExecutionTicketRecord | None:
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit:
                row = await unit.session.scalar(
                    select(ExecutionTicketModel).where(
                        ExecutionTicketModel.tenant_id == UUID(context.tenant_id),
                        ExecutionTicketModel.approval_id == approval_id,
                    )
                )
                return _ticket_record(row) if row is not None else None
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

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
    ) -> MutationOutcome[ApprovalRequestRecord] | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                session = unit.session
                row = await session.scalar(
                    select(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.tenant_id == tenant_id,
                        ApprovalRequestModel.id == approval_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    operation_type="approval.decision",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return MutationOutcome(replay=replay)
                if row.resource_version != expected_version:
                    raise resource_version_conflict()
                if row.status == "PENDING" and row.expires_at <= now:
                    await _expire_one(session, context, row=row, now=now)
                    raise resource_state_conflict("The Approval has expired.")
                if row.status != "PENDING":
                    raise resource_state_conflict(
                        "The Approval has already been resolved."
                    )
                if not row.self_approval_allowed and row.requester_id == actor_id:
                    raise resource_state_conflict(
                        "The Approval requester cannot decide this request."
                    )
                row.status = request.decision
                row.resource_version += 1
                row.updated_at = now
                # The Decision trigger validates the already-resolved request state.
                # Flush the guarded state transition before inserting its immutable fact.
                await session.flush()
                decision = ApprovalDecisionModel(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"approval-decision/{tenant_id}/{approval_id}/{actor_id}/"
                        f"{idempotency_key}",
                    ),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    actor_id=actor_id,
                    decision=request.decision,
                    comment=request.comment,
                    created_at=now,
                )
                session.add(decision)
                if request.decision == "APPROVED" and ticket_issue is not None:
                    session.add(
                        ExecutionTicketModel(
                            id=ticket_issue.credential.ticket_id,
                            tenant_id=tenant_id,
                            approval_id=row.id,
                            run_id=row.run_id,
                            execution_attempt=row.execution_attempt,
                            requester_id=row.requester_id,
                            tool_name=row.tool_name,
                            tool_schema_hash=row.tool_schema_hash,
                            parameter_digest=row.parameter_digest,
                            policy_version=row.policy_version,
                            deployment_id=row.deployment_id,
                            nonce_hash=ticket_issue.credential.nonce_hash,
                            expires_at=min(row.expires_at, ticket_issue.expires_at),
                            single_use=True,
                            created_at=now,
                        )
                    )
                run = await session.scalar(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == row.run_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise resource_state_conflict("The Run is unavailable.")
                if request.decision == "REJECTED" and run.status == "WAITING_APPROVAL":
                    ensure_run_transition("WAITING_APPROVAL", "CANCELLING")
                    run.status = "CANCELLING"
                candidate = RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                    {
                        "source_event_id": f"approval-resolved:{approval_id}:{row.resource_version}",
                        "event_type": "approval_resolved",
                        "occurred_at": now,
                        "payload_version": "1.0",
                        "payload": {
                            "approval_id": str(approval_id),
                            "decision": request.decision,
                            "decided_by": str(actor_id),
                        },
                    }
                )
                await append_control_run_event(
                    session,
                    context,
                    run=run,
                    execution_attempt=row.execution_attempt,
                    candidate=candidate,
                )
                _audit(
                    session,
                    context,
                    action=(
                        "approval.approve"
                        if request.decision == "APPROVED"
                        else "approval.reject"
                    ),
                    approval_id=approval_id,
                    result="SUCCESS",
                    reason_codes=[],
                    metadata={
                        "approval_id": str(approval_id),
                        "decision": request.decision,
                    },
                    occurred_at=now,
                )
                record = _approval_record(row)
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=200,
                    response_body={
                        "id": str(record.id),
                        "run_id": str(record.run_id),
                        "tool_name": record.tool_name,
                        "parameter_digest": record.parameter_digest,
                        "status": record.status,
                        "expires_at": record.expires_at.isoformat(),
                        "resource_version": record.resource_version,
                    },
                    response_etag=f'"rv:{record.resource_version}"',
                    response_ref=str(record.id),
                )
                return MutationOutcome(value=record)
        except (IntegrityError, SQLAlchemyError) as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error

    async def expire_due(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ApprovalRequestRecord, ...]:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit:
                return await _expire_due_in_transaction(
                    unit.session, context, now=now, limit=limit
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Approval Store is unavailable.") from error


async def _expire_due_in_transaction(
    session: AsyncSession,
    context: TenantContext,
    *,
    now: datetime,
    limit: int,
) -> tuple[ApprovalRequestRecord, ...]:
    rows = list(
        (
            await session.scalars(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.tenant_id == UUID(context.tenant_id),
                    ApprovalRequestModel.status == "PENDING",
                    ApprovalRequestModel.expires_at <= now,
                )
                .order_by(ApprovalRequestModel.expires_at, ApprovalRequestModel.id)
                .limit(limit)
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        await _expire_one(session, context, row=row, now=now)
    return tuple(_approval_record(row) for row in rows)


async def _expire_one(
    session: AsyncSession,
    context: TenantContext,
    *,
    row: ApprovalRequestModel,
    now: datetime,
) -> None:
    if row.status != "PENDING":
        return
    row.status = "EXPIRED"
    row.resource_version += 1
    row.updated_at = now
    run = await session.scalar(
        select(AgentRunModel)
        .where(
            AgentRunModel.tenant_id == row.tenant_id,
            AgentRunModel.id == row.run_id,
        )
        .with_for_update()
    )
    if run is not None and run.status == "WAITING_APPROVAL":
        ensure_run_transition("WAITING_APPROVAL", "TIMEOUT")
        run.status = "TIMEOUT"
        run.error_code = "APPROVAL_EXPIRED"
        run.finished_at = now
    candidate = RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
        {
            "source_event_id": f"approval-resolved:{row.id}:{row.resource_version}",
            "event_type": "approval_resolved",
            "occurred_at": now,
            "payload_version": "1.0",
            "payload": {
                "approval_id": str(row.id),
                "decision": "EXPIRED",
                "decided_by": str(_EXPIRY_ACTOR_ID),
            },
        }
    )
    if run is not None:
        await append_control_run_event(
            session,
            context,
            run=run,
            execution_attempt=row.execution_attempt,
            candidate=candidate,
        )
    _audit(
        session,
        context,
        action="approval.expire",
        approval_id=row.id,
        result="SUCCESS",
        reason_codes=["APPROVAL_EXPIRED"],
        metadata={"approval_id": str(row.id)},
        occurred_at=now,
    )


def _same_request(row: ApprovalRequestModel, request: ApprovalRequestInput) -> bool:
    return (
        row.run_id == request.run_id
        and row.execution_attempt == request.execution_attempt
        and row.requester_id == request.requester_id
        and row.tool_call_id == request.tool_call_id
        and row.tool_name == request.tool_name
        and row.tool_schema_hash == request.tool_schema_hash
        and row.parameter_digest == request.parameter_digest
        and row.policy_version == request.policy_version
        and row.deployment_id == request.deployment_id
        and row.expires_at == request.expires_at
        and row.self_approval_allowed == request.self_approval_allowed
    )


def _approval_record(row: ApprovalRequestModel) -> ApprovalRequestRecord:
    return ApprovalRequestRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        execution_attempt=row.execution_attempt,
        requester_id=row.requester_id,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        tool_schema_hash=row.tool_schema_hash,
        parameter_digest=row.parameter_digest,
        policy_version=row.policy_version,
        deployment_id=row.deployment_id,
        status=cast(ApprovalStatus, row.status),
        expires_at=row.expires_at,
        resource_version=row.resource_version,
        self_approval_allowed=row.self_approval_allowed,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _decision_record(row: ApprovalDecisionModel) -> ApprovalDecisionRecord:
    return ApprovalDecisionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        approval_id=row.approval_id,
        actor_id=row.actor_id,
        decision=cast(ApprovalDecision, row.decision),
        comment=row.comment,
        created_at=row.created_at,
    )


def _ticket_record(row: ExecutionTicketModel) -> ExecutionTicketRecord:
    return ExecutionTicketRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        approval_id=row.approval_id,
        run_id=row.run_id,
        execution_attempt=row.execution_attempt,
        requester_id=row.requester_id,
        tool_name=row.tool_name,
        tool_schema_hash=row.tool_schema_hash,
        parameter_digest=row.parameter_digest,
        policy_version=row.policy_version,
        deployment_id=row.deployment_id,
        nonce_hash=row.nonce_hash,
        expires_at=row.expires_at,
        single_use=row.single_use,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )


def _audit(
    session: AsyncSession,
    context: TenantContext,
    *,
    action: str,
    approval_id: UUID,
    result: str,
    reason_codes: list[str],
    metadata: dict[str, object],
    occurred_at: datetime,
) -> None:
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            id=uuid5(
                NAMESPACE_URL,
                f"audit/{context.trace_id}/{action}/{approval_id}/"
                f"{occurred_at.isoformat()}",
            ),
            tenant_id=UUID(context.tenant_id),
            actor_type=context.subject_type.value,
            actor_id=UUID(context.subject_id),
            action=action,
            resource_type="approval",
            resource_id=approval_id,
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
