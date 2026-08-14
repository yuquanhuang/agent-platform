"""PostgreSQL Run persistence with atomic Session, queue and Outbox facts."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.metadata import RequestMetadata
from packages.application.outbox import PermanentOutboxError, RetryableOutboxError
from packages.application.outbox.run import WorkflowStartOutcome
from packages.application.policy import (
    AdmissionDenied,
    CapacityAdmissionDenied,
    RunCapacityFacts,
    RunCapacityPolicy,
    RuntimeBundleAdmissionFacts,
    admit_run_capacity,
    admit_runtime_bundle,
)
from packages.application.reconciliation import RunReconciliationCandidate
from packages.application.runs import RUN_REQUESTED_EVENT
from packages.application.temporal.run_activities import (
    RunSpecCompilationSource,
    RunStageError,
)
from packages.contracts.generated.core_models import (
    CancelRunRequest,
    RetryRunRequest,
    RunCreateRequest,
)
from packages.contracts.generated.run_event import RUNTIME_EVENT_CANDIDATE_ADAPTER
from packages.contracts.public import (
    TenantContext,
    rate_limited,
    resource_state_conflict,
    run_already_active,
    validation_error,
)
from packages.contracts.temporal import (
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    FinalizeAgentRunResult,
    RunRequestedPayloadV1,
)
from packages.domain.public import (
    EXECUTION_CAPACITY_RUN_STATUSES,
    NON_TERMINAL_RUN_STATUSES,
    MutationOutcome,
    OutboxEvent,
    OutboxStatus,
    RunAttemptRecord,
    RunRecord,
    RunStatus,
    decode_cursor,
    encode_cursor,
    ensure_run_attempt_transition,
    ensure_run_transition,
    parse_message_content_parts,
)
from packages.infrastructure.database.events import append_control_run_event
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentDefinitionModel,
    AgentRunModel,
    AgentSnapshotModel,
    AgentVersionModel,
    AuditLogModel,
    ChatMessageModel,
    ChatSessionModel,
    DeploymentModel,
    OutboxEventModel,
    RunAdmissionQueueModel,
    RunAttemptModel,
    RunCapacityDomainModel,
    RunCapacityLeaseModel,
    RuntimeBundleModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.quota_policies import (
    load_active_run_capacity_policy_version,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork, TenantUnitOfWork


class SqlAlchemyRunStore:
    """Persist immutable Run inputs and current lifecycle materialization."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        run_capacity_policy: RunCapacityPolicy | None = None,
        queue_max_wait: timedelta = timedelta(minutes=5),
        queue_max_pending_per_tenant: int = 1_000,
        capacity_domain_slots: Mapping[str, int] | None = None,
        capacity_lease_ttl: timedelta = timedelta(minutes=5),
        capacity_tenant_quantum: int = 1,
    ) -> None:
        if queue_max_wait <= timedelta(0) or queue_max_wait > timedelta(days=1):
            raise ValueError("Run queue max wait must be between 1 second and 1 day")
        if not 1 <= queue_max_pending_per_tenant <= 1_000_000:
            raise ValueError("Run queue pending limit must be between 1 and 1000000")
        if capacity_lease_ttl < timedelta(seconds=30) or capacity_lease_ttl > timedelta(
            days=1
        ):
            raise ValueError(
                "Run capacity lease TTL must be between 30 seconds and 1 day"
            )
        if not 1 <= capacity_tenant_quantum <= 100:
            raise ValueError("Run capacity tenant quantum must be between 1 and 100")
        self._session_factory = session_factory
        self._run_capacity_policy = run_capacity_policy or RunCapacityPolicy()
        self._queue_max_wait = queue_max_wait
        self._queue_max_pending_per_tenant = queue_max_pending_per_tenant
        self._capacity_domain_slots = dict(capacity_domain_slots or {})
        self._capacity_lease_ttl = capacity_lease_ttl
        self._capacity_tenant_quantum = capacity_tenant_quantum

    async def _record_capacity_denial(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        resource_id: UUID | None,
        operation: Literal["create", "retry"],
        denial: CapacityAdmissionDenied,
        metadata: RequestMetadata,
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            _capacity_denial_audit(
                unit_of_work.session,
                tenant_id=UUID(context.tenant_id),
                actor_id=actor_id,
                resource_id=resource_id,
                operation=operation,
                denial=denial,
                metadata=metadata,
            )

    async def _enqueue_run(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run: AgentRunModel,
        now: datetime,
    ) -> RunAdmissionQueueModel:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(" "hashtextextended(:lock_key, 0))"),
            {"lock_key": f"run-queue:tenant:{tenant_id}"},
        )
        pending = await session.scalar(
            select(func.count(RunAdmissionQueueModel.id)).where(
                RunAdmissionQueueModel.tenant_id == tenant_id,
                RunAdmissionQueueModel.status == "WAITING",
            )
        )
        if int(pending or 0) >= self._queue_max_pending_per_tenant:
            raise rate_limited(
                details={
                    "scope": "tenant",
                    "reason_code": "RUN_QUEUE_PENDING_LIMIT",
                }
            )
        capacity_domain = await session.scalar(
            select(DeploymentModel.runtime_target_id).where(
                DeploymentModel.tenant_id == tenant_id,
                DeploymentModel.id == run.deployment_id,
            )
        )
        if capacity_domain is None:
            raise resource_state_conflict(
                "The Run Deployment capacity domain is unavailable."
            )
        queue = RunAdmissionQueueModel(
            id=uuid5(NAMESPACE_URL, f"run-admission/{tenant_id}/{run.id}"),
            tenant_id=tenant_id,
            run_id=run.id,
            priority="NORMAL",
            capacity_domain=capacity_domain,
            status="WAITING",
            queued_at=now,
            deadline_at=now + self._queue_max_wait,
            admitted_at=None,
            cancelled_at=None,
            quota_policy_version_id=None,
            capacity_snapshot_json=None,
            resource_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(queue)
        return queue

    async def process_admission_queue(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[int, int]:
        """Expire due entries, then admit a bounded priority/FIFO tenant batch."""

        if not 1 <= limit <= 500:
            raise ValueError("Run queue admission limit must be between 1 and 500")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            expired_rows = list(
                await session.scalars(
                    select(RunAdmissionQueueModel)
                    .where(
                        RunAdmissionQueueModel.tenant_id == tenant_id,
                        RunAdmissionQueueModel.status == "WAITING",
                        RunAdmissionQueueModel.deadline_at <= now,
                    )
                    .order_by(
                        RunAdmissionQueueModel.deadline_at,
                        RunAdmissionQueueModel.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            expired = 0
            for queue in expired_rows:
                run = (
                    await session.execute(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.tenant_id == tenant_id,
                            AgentRunModel.id == queue.run_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None or run.status != "QUEUED":
                    continue
                queue.status = "TIMED_OUT"
                queue.cancelled_at = now
                queue.updated_at = now
                queue.resource_version += 1
                ensure_run_transition("QUEUED", "TIMEOUT")
                run.status = "TIMEOUT"
                run.error_code = "RUN_QUEUE_TIMEOUT"
                run.error_detail_json = {
                    "message": "The Run exceeded its admission queue deadline.",
                    "request_id": context.request_id,
                    "retryable": True,
                    "stage": "queue",
                }
                run.finished_at = now
                await append_control_run_event(
                    session,
                    context,
                    run=run,
                    execution_attempt=1,
                    candidate=RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": f"queue-timeout:{run.id}",
                            "event_type": "run_timeout",
                            "occurred_at": now,
                            "payload_version": "1.0",
                            "payload": {
                                "timeout_seconds": max(
                                    1,
                                    int(
                                        (
                                            queue.deadline_at - queue.queued_at
                                        ).total_seconds()
                                    ),
                                ),
                                "stage": "queue",
                            },
                        }
                    ),
                )
                _reconciliation_audit(
                    session,
                    context=context,
                    run_id=run.id,
                    action="run.queue.timed_out",
                    change={"deadline_at": queue.deadline_at.isoformat()},
                    occurred_at=now,
                )
                expired += 1

            remaining = limit - expired
            if remaining <= 0:
                return 0, expired
            candidates = list(
                await session.scalars(
                    select(RunAdmissionQueueModel)
                    .join(
                        AgentRunModel,
                        and_(
                            AgentRunModel.tenant_id == RunAdmissionQueueModel.tenant_id,
                            AgentRunModel.id == RunAdmissionQueueModel.run_id,
                        ),
                    )
                    .where(
                        RunAdmissionQueueModel.tenant_id == tenant_id,
                        RunAdmissionQueueModel.status == "WAITING",
                        RunAdmissionQueueModel.deadline_at > now,
                    )
                    .order_by(
                        (RunAdmissionQueueModel.priority == "HIGH").desc(),
                        RunAdmissionQueueModel.queued_at,
                        RunAdmissionQueueModel.id,
                    )
                    .limit(remaining)
                    .with_for_update(of=RunAdmissionQueueModel, skip_locked=True)
                )
            )
            admitted = 0
            for queue in candidates:
                run = (
                    await session.execute(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.tenant_id == tenant_id,
                            AgentRunModel.id == queue.run_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None or run.status != "QUEUED" or run.workflow_id is not None:
                    continue
                deployment = await session.scalar(
                    select(DeploymentModel).where(
                        DeploymentModel.tenant_id == tenant_id,
                        DeploymentModel.id == run.deployment_id,
                    )
                )
                if deployment is None:
                    continue
                try:
                    policy_version_id, effective_policy, runtime_type = (
                        await _admit_run_capacity(
                            session,
                            policy=self._run_capacity_policy,
                            tenant_id=tenant_id,
                            user_id=run.created_by,
                            agent_id=run.agent_id,
                            deployment=deployment,
                        )
                    )
                except CapacityAdmissionDenied:
                    break
                queue.status = "ADMITTED"
                queue.admitted_at = now
                queue.quota_policy_version_id = policy_version_id
                queue.capacity_snapshot_json = cast(
                    dict[str, object],
                    _capacity_snapshot(effective_policy, runtime_type),
                )
                queue.updated_at = now
                queue.resource_version += 1
                payload = RunRequestedPayloadV1(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                )
                SqlAlchemyOutboxWriter(session, context).add(
                    OutboxEvent(
                        id=uuid5(NAMESPACE_URL, f"run-outbox/{tenant_id}/{run.id}"),
                        tenant_id=tenant_id,
                        aggregate_type="run",
                        aggregate_id=run.id,
                        event_type=RUN_REQUESTED_EVENT,
                        payload=payload.model_dump(mode="json"),
                        payload_schema_version=1,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
                _reconciliation_audit(
                    session,
                    context=context,
                    run_id=run.id,
                    action="run.queue.admitted",
                    change={
                        "capacity_domain": queue.capacity_domain,
                        "runtime_type": runtime_type,
                        "quota_policy_version_id": (
                            str(policy_version_id) if policy_version_id else None
                        ),
                    },
                    occurred_at=now,
                )
                admitted += 1
                # The next candidate must observe this batch's reserved slot.
                await session.flush()
            await session.flush()
            return admitted, expired

    async def process_capacity_admission_domains(
        self,
        scheduler_context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[int, int, int, int]:
        """Reconcile leases and admit a fair cross-tenant domain batch."""

        if not 1 <= limit <= 500:
            raise ValueError("Run queue admission limit must be between 1 and 500")
        if not self._capacity_domain_slots:
            admitted, timed_out = await self.process_admission_queue(
                scheduler_context, now=now, limit=limit
            )
            return admitted, timed_out, 0, 0

        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            scheduler_lock = await session.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended('run-capacity-domain-scheduler', 0))"
                )
            )
            if scheduler_lock is not True:
                return 0, 0, 0, 0
            await _sync_capacity_domains(
                session, configured=self._capacity_domain_slots, now=now
            )
            released, renewed = await _reconcile_capacity_leases(
                session, now=now, lease_ttl=self._capacity_lease_ttl, limit=limit
            )
            timed_out = await _expire_capacity_queue_entries(
                session,
                scheduler_context=scheduler_context,
                now=now,
                limit=limit,
            )
            remaining = limit - timed_out
            if remaining <= 0:
                return 0, timed_out, released, renewed
            ranked_queue = _ranked_capacity_candidates(
                now=now,
                capacity_domains=tuple(self._capacity_domain_slots),
            )
            candidates = list(
                await session.scalars(
                    select(RunAdmissionQueueModel)
                    .where(
                        RunAdmissionQueueModel.id.in_(
                            select(ranked_queue.c.queue_id).where(
                                ranked_queue.c.tenant_position
                                <= self._capacity_tenant_quantum
                            )
                        )
                    )
                    .order_by(
                        RunAdmissionQueueModel.capacity_domain,
                        (RunAdmissionQueueModel.priority == "HIGH").desc(),
                        RunAdmissionQueueModel.queued_at,
                        RunAdmissionQueueModel.id,
                    )
                    .limit(500)
                    .with_for_update(skip_locked=True)
                )
            )
            last_rows = (
                await session.execute(
                    select(
                        RunAdmissionQueueModel.capacity_domain,
                        RunAdmissionQueueModel.tenant_id,
                        func.max(RunAdmissionQueueModel.admitted_at),
                    )
                    .where(RunAdmissionQueueModel.status == "ADMITTED")
                    .group_by(
                        RunAdmissionQueueModel.capacity_domain,
                        RunAdmissionQueueModel.tenant_id,
                    )
                )
            ).all()
            ordered = _fair_capacity_candidates(
                candidates,
                last_admitted={
                    (domain, tenant): value for domain, tenant, value in last_rows
                },
                tenant_quantum=self._capacity_tenant_quantum,
            )
            admitted = 0
            for queue in ordered:
                if admitted >= remaining:
                    break
                await _bind_scheduler_tenant(session, queue.tenant_id)
                context = _scheduler_tenant_context(
                    scheduler_context, tenant_id=queue.tenant_id
                )
                run = await session.scalar(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.tenant_id == queue.tenant_id,
                        AgentRunModel.id == queue.run_id,
                    )
                    .with_for_update()
                )
                if run is None or run.status != "QUEUED" or run.workflow_id is not None:
                    continue
                deployment = await session.scalar(
                    select(DeploymentModel).where(
                        DeploymentModel.tenant_id == queue.tenant_id,
                        DeploymentModel.id == run.deployment_id,
                    )
                )
                domain = await session.scalar(
                    select(RunCapacityDomainModel)
                    .where(RunCapacityDomainModel.domain_key == queue.capacity_domain)
                    .with_for_update()
                )
                if (
                    deployment is None
                    or deployment.runtime_target_id != queue.capacity_domain
                    or domain is None
                    or domain.status != "ACTIVE"
                ):
                    continue
                active = int(
                    await session.scalar(
                        select(func.count(RunCapacityLeaseModel.id)).where(
                            RunCapacityLeaseModel.domain_key == domain.domain_key,
                            RunCapacityLeaseModel.released_at.is_(None),
                        )
                    )
                    or 0
                )
                if active >= domain.configured_slots:
                    continue
                try:
                    policy_version_id, effective_policy, runtime_type = (
                        await _admit_run_capacity(
                            session,
                            policy=self._run_capacity_policy,
                            tenant_id=queue.tenant_id,
                            user_id=run.created_by,
                            agent_id=run.agent_id,
                            deployment=deployment,
                        )
                    )
                except CapacityAdmissionDenied:
                    continue
                lease = RunCapacityLeaseModel(
                    id=uuid5(NAMESPACE_URL, f"run-capacity-lease/{queue.run_id}"),
                    domain_key=domain.domain_key,
                    tenant_id=queue.tenant_id,
                    run_id=queue.run_id,
                    acquired_at=now,
                    renewed_at=now,
                    expires_at=now + self._capacity_lease_ttl,
                    released_at=None,
                    release_reason=None,
                    resource_version=1,
                )
                session.add(lease)
                queue.status = "ADMITTED"
                queue.admitted_at = now
                queue.quota_policy_version_id = policy_version_id
                snapshot = _capacity_snapshot(effective_policy, runtime_type)
                snapshot.update(
                    {
                        "capacity_domain": domain.domain_key,
                        "capacity_domain_config_hash": domain.config_hash,
                        "capacity_domain_slots": domain.configured_slots,
                        "capacity_lease_id": str(lease.id),
                        "capacity_lease_expires_at": lease.expires_at.isoformat(),
                    }
                )
                queue.capacity_snapshot_json = cast(dict[str, object], snapshot)
                queue.updated_at = now
                queue.resource_version += 1
                SqlAlchemyOutboxWriter(session, context).add(
                    OutboxEvent(
                        id=uuid5(
                            NAMESPACE_URL,
                            f"run-outbox/{queue.tenant_id}/{queue.run_id}",
                        ),
                        tenant_id=queue.tenant_id,
                        aggregate_type="run",
                        aggregate_id=queue.run_id,
                        event_type=RUN_REQUESTED_EVENT,
                        payload=RunRequestedPayloadV1(
                            tenant_id=queue.tenant_id,
                            run_id=queue.run_id,
                            request_id=context.request_id,
                            trace_id=context.trace_id,
                        ).model_dump(mode="json"),
                        payload_schema_version=1,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
                _reconciliation_audit(
                    session,
                    context=context,
                    run_id=run.id,
                    action="run.queue.admitted",
                    change={
                        "capacity_domain": domain.domain_key,
                        "capacity_lease_id": str(lease.id),
                        "runtime_type": runtime_type,
                    },
                    occurred_at=now,
                )
                admitted += 1
                await session.flush()
            return admitted, timed_out, released, renewed

    async def record_workflow_start(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        workflow_id: str,
        temporal_run_id: str,
        outcome: WorkflowStartOutcome,
        started_at: datetime,
    ) -> bool:
        if not workflow_id or len(workflow_id) > 255:
            raise PermanentOutboxError("Temporal Workflow ID is invalid")
        if not temporal_run_id or len(temporal_run_id) > 255:
            raise PermanentOutboxError("Temporal Run ID is unavailable")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
                run = await unit_of_work.session.scalar(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == run_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise PermanentOutboxError("The Run start target is unavailable")
                if run.workflow_id is not None and run.workflow_id != workflow_id:
                    raise PermanentOutboxError(
                        "The Run is already bound to another Workflow"
                    )
                if run.temporal_run_id is not None:
                    if run.workflow_id != workflow_id:
                        raise PermanentOutboxError(
                            "The Run Workflow mapping conflicts with persisted facts"
                        )
                    return False
                run.workflow_id = workflow_id
                run.temporal_run_id = temporal_run_id
                run.workflow_start_outcome = outcome
                run.workflow_started_at = started_at
                _reconciliation_audit(
                    unit_of_work.session,
                    context=context,
                    run_id=run.id,
                    action="run.workflow.mapping.recorded",
                    change={
                        "workflow_id": workflow_id,
                        "temporal_run_id": temporal_run_id,
                        "outcome": outcome,
                    },
                    occurred_at=started_at,
                )
                await unit_of_work.session.flush()
                return True
        except PermanentOutboxError:
            raise
        except SQLAlchemyError as error:
            raise RetryableOutboxError(
                "The Run Workflow mapping could not be persisted"
            ) from error

    async def list_stalled_runs(
        self,
        context: TenantContext,
        *,
        created_before: datetime,
        cancelling_before: datetime,
        limit: int,
    ) -> tuple[RunReconciliationCandidate, ...]:
        if limit < 1:
            raise ValueError("Run reconciliation limit must be positive")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            rows = await unit_of_work.session.scalars(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    or_(
                        and_(
                            AgentRunModel.status == "CREATED",
                            AgentRunModel.created_at <= created_before,
                        ),
                        and_(
                            AgentRunModel.status == "QUEUED",
                            AgentRunModel.queued_at <= created_before,
                            or_(
                                AgentRunModel.workflow_id.is_not(None),
                                exists(
                                    select(RunAdmissionQueueModel.id).where(
                                        RunAdmissionQueueModel.tenant_id
                                        == AgentRunModel.tenant_id,
                                        RunAdmissionQueueModel.run_id
                                        == AgentRunModel.id,
                                        RunAdmissionQueueModel.status == "ADMITTED",
                                    )
                                ),
                            ),
                        ),
                        and_(
                            AgentRunModel.status == "CANCELLING",
                            AgentRunModel.cancelling_at.is_not(None),
                            AgentRunModel.cancelling_at <= cancelling_before,
                        ),
                    ),
                )
                .order_by(AgentRunModel.created_at, AgentRunModel.id)
                .limit(limit)
            )
            return tuple(
                RunReconciliationCandidate(
                    run_id=row.id,
                    tenant_id=row.tenant_id,
                    status=cast(Literal["CREATED", "QUEUED", "CANCELLING"], row.status),
                    created_by=row.created_by,
                    workflow_id=row.workflow_id,
                    temporal_run_id=row.temporal_run_id,
                )
                for row in rows
            )

    async def requeue_run_request(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        now: datetime,
    ) -> bool:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                )
                .with_for_update()
            )
            if run is None or run.status not in {"CREATED", "QUEUED", "CANCELLING"}:
                return False
            if run.status == "QUEUED":
                queue_status = await session.scalar(
                    select(RunAdmissionQueueModel.status).where(
                        RunAdmissionQueueModel.tenant_id == tenant_id,
                        RunAdmissionQueueModel.run_id == run_id,
                    )
                )
                if queue_status != "ADMITTED":
                    return False
            event = await session.scalar(
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.tenant_id == tenant_id,
                    OutboxEventModel.aggregate_type == "run",
                    OutboxEventModel.aggregate_id == run_id,
                    OutboxEventModel.event_type == RUN_REQUESTED_EVENT,
                )
                .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
                .limit(1)
                .with_for_update()
            )
            if event is None:
                request_id = f"reconcile-run-{run_id}"
                event = OutboxEventModel(
                    id=uuid5(NAMESPACE_URL, f"run-outbox/{tenant_id}/{run_id}"),
                    tenant_id=tenant_id,
                    aggregate_type="run",
                    aggregate_id=run_id,
                    event_type=RUN_REQUESTED_EVENT,
                    payload_json=RunRequestedPayloadV1(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        request_id=request_id,
                        trace_id=request_id,
                    ).model_dump(mode="json"),
                    payload_schema_version=1,
                    status=OutboxStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    published_at=None,
                )
                session.add(event)
                _reconciliation_audit(
                    session,
                    context=context,
                    run_id=run.id,
                    action="run.reconcile.request_requeued",
                    change={"outbox_recreated": True},
                    occurred_at=now,
                )
                await session.flush()
                return True
            if (
                event.status == OutboxStatus.PENDING.value
                and event.next_attempt_at <= now
            ):
                return False
            if (
                event.status == OutboxStatus.PUBLISHING.value
                and event.next_attempt_at > now
            ):
                return False
            event.status = OutboxStatus.PENDING.value
            event.attempts = 0
            event.next_attempt_at = now
            event.published_at = None
            _reconciliation_audit(
                session,
                context=context,
                run_id=run.id,
                action="run.reconcile.request_requeued",
                change={"outbox_recreated": False},
                occurred_at=now,
            )
            await session.flush()
            return True

    async def record_cancel_signal_delivery(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        signal_id: str,
        now: datetime,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            run_exists = await unit_of_work.session.scalar(
                select(AgentRunModel.id).where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                )
            )
            if run_exists is None:
                raise PermanentOutboxError("The reconciled Run is unavailable")
            _reconciliation_audit(
                unit_of_work.session,
                context=context,
                run_id=run_id,
                action="run.reconcile.cancel_signal_sent",
                change={"signal_id": signal_id},
                occurred_at=now,
            )
            await unit_of_work.session.flush()

    async def list_session_runs(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[RunRecord], str | None] | None:
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            owned = await unit_of_work.session.scalar(
                select(ChatSessionModel.id).where(
                    ChatSessionModel.tenant_id == tenant_id,
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.user_id == user_id,
                    ChatSessionModel.status != "DELETED",
                )
            )
            if owned is None:
                return None
            statement = (
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.session_id == session_id,
                )
                .order_by(AgentRunModel.created_at.desc(), AgentRunModel.id.desc())
            )
            if cursor is not None:
                try:
                    created_at, run_id = decode_cursor(cursor)
                except ValueError as exc:
                    raise validation_error("Pagination cursor is invalid.") from exc
                statement = statement.where(
                    or_(
                        AgentRunModel.created_at < created_at,
                        and_(
                            AgentRunModel.created_at == created_at,
                            AgentRunModel.id < run_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.scalars(statement.limit(limit + 1))).all()
            )
            page = rows[:limit]
            next_cursor = (
                encode_cursor(page[-1].created_at, page[-1].id)
                if len(rows) > limit and page
                else None
            )
            return [_run_record(row) for row in page], next_cursor

    async def create_run(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        request: RunCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord]:
        if request.input.attachments:
            raise resource_state_conflict(
                "Run attachments are unavailable until governed Artifact facts exist."
            )
        tenant_id = UUID(context.tenant_id)
        session_id = _resource_id(request.session_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
                session = unit_of_work.session
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    operation_type="run.create",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return MutationOutcome(replay=replay)
                chat_session = await session.scalar(
                    select(ChatSessionModel)
                    .where(
                        ChatSessionModel.tenant_id == tenant_id,
                        ChatSessionModel.id == session_id,
                        ChatSessionModel.user_id == user_id,
                    )
                    .with_for_update()
                )
                if chat_session is None:
                    raise validation_error("Session identifier is invalid.")
                if chat_session.status != "ACTIVE":
                    raise resource_state_conflict(
                        "Runs can only be created for an active Session."
                    )
                parent = None
                branch_id = chat_session.branch_root_id
                if chat_session.cursor_message_id is not None:
                    parent = await session.scalar(
                        select(ChatMessageModel).where(
                            ChatMessageModel.tenant_id == tenant_id,
                            ChatMessageModel.session_id == session_id,
                            ChatMessageModel.id == chat_session.cursor_message_id,
                        )
                    )
                    if parent is None:
                        raise resource_state_conflict(
                            "The Session message cursor is inconsistent."
                        )
                    branch_id = parent.branch_id
                active_run = await session.scalar(
                    select(AgentRunModel.id).where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.session_id == session_id,
                        AgentRunModel.branch_id.is_not_distinct_from(branch_id),
                        AgentRunModel.status.in_(NON_TERMINAL_RUN_STATUSES),
                    )
                )
                if active_run is not None:
                    raise run_already_active()
                deployment = await self._deployment(
                    session,
                    tenant_id=tenant_id,
                    chat_session=chat_session,
                    requested_id=(
                        request.execution.deployment_id
                        if request.execution is not None
                        else None
                    ),
                )
                now = datetime.now(UTC)
                user_message = ChatMessageModel(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    branch_id=branch_id,
                    parent_message_id=parent.id if parent is not None else None,
                    role="USER",
                    content_parts_json=[{"type": "text", "text": request.input.text}],
                    source_run_id=None,
                    created_at=now,
                    created_by=user_id,
                )
                session.add(user_message)
                await session.flush()
                execution = request.execution
                cost_budget = execution.cost_budget if execution is not None else None
                run = AgentRunModel(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    branch_id=branch_id,
                    user_message_id=user_message.id,
                    assistant_message_id=None,
                    agent_id=chat_session.agent_id,
                    snapshot_id=deployment.snapshot_id,
                    deployment_id=deployment.id,
                    status="QUEUED",
                    current_attempt=0,
                    latest_sequence_no=0,
                    idempotency_key=idempotency_key,
                    client_request_id=request.client_request_id,
                    retry_of_run_id=None,
                    timeout_seconds=(
                        execution.timeout_seconds
                        if execution is not None
                        and execution.timeout_seconds is not None
                        else 600
                    ),
                    token_budget=(
                        execution.token_budget if execution is not None else None
                    ),
                    cost_budget_amount=(
                        Decimal(cost_budget.amount) if cost_budget is not None else None
                    ),
                    cost_budget_currency=(
                        cost_budget.currency if cost_budget is not None else None
                    ),
                    created_by=user_id,
                    created_at=now,
                    queued_at=now,
                )
                session.add(run)
                await session.flush()
                await self._enqueue_run(
                    session,
                    tenant_id=tenant_id,
                    run=run,
                    now=now,
                )
                chat_session.cursor_message_id = user_message.id
                chat_session.updated_at = now
                chat_session.resource_version += 1
                result = _run_record(run)
                accepted = _accepted_json(result)
                await _audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    run_id=run.id,
                    metadata=metadata,
                    change={
                        "session_id": str(session_id),
                        "deployment_id": str(deployment.id),
                        "snapshot_id": str(deployment.snapshot_id),
                        "user_message_id": str(user_message.id),
                        "branch_id": str(branch_id) if branch_id else None,
                        "input_text_length": len(request.input.text),
                        "has_token_budget": run.token_budget is not None,
                        "has_cost_budget": run.cost_budget_amount is not None,
                        "queue_priority": "NORMAL",
                        "queue_deadline_at": (now + self._queue_max_wait).isoformat(),
                    },
                )
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=202,
                    response_body=accepted,
                    response_etag=None,
                    response_ref=str(run.id),
                )
                return MutationOutcome(value=result)
        except IntegrityError as exc:
            constraint = getattr(
                getattr(exc.orig, "diag", None), "constraint_name", None
            )
            if constraint == "uq_agent_run__active_session_branch":
                raise run_already_active() from exc
            raise

    async def get_run(
        self, context: TenantContext, *, user_id: UUID, run_id: UUID
    ) -> RunRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(AgentRunModel)
                .join(
                    ChatSessionModel,
                    and_(
                        ChatSessionModel.tenant_id == AgentRunModel.tenant_id,
                        ChatSessionModel.id == AgentRunModel.session_id,
                    ),
                )
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                    ChatSessionModel.user_id == user_id,
                    ChatSessionModel.status != "DELETED",
                )
            )
            return _run_record(model) if model is not None else None

    async def request_cancel(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        request: CancelRunRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            idempotency_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                operation_type="run.cancel",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            run = await session.scalar(
                select(AgentRunModel)
                .join(
                    ChatSessionModel,
                    and_(
                        ChatSessionModel.tenant_id == AgentRunModel.tenant_id,
                        ChatSessionModel.id == AgentRunModel.session_id,
                    ),
                )
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                    ChatSessionModel.user_id == user_id,
                    ChatSessionModel.status != "DELETED",
                )
                .with_for_update()
            )
            if run is None:
                return None
            queue = None
            if run.status == "QUEUED" and run.workflow_id is None:
                queue = await session.scalar(
                    select(RunAdmissionQueueModel)
                    .where(
                        RunAdmissionQueueModel.tenant_id == tenant_id,
                        RunAdmissionQueueModel.run_id == run.id,
                    )
                    .with_for_update()
                )
            if queue is not None and queue.status == "WAITING":
                now = datetime.now(UTC)
                ensure_run_transition("QUEUED", "CANCELLED")
                run.status = "CANCELLED"
                run.finished_at = now
                queue.status = "CANCELLED"
                queue.cancelled_at = now
                queue.updated_at = now
                queue.resource_version += 1
                await append_control_run_event(
                    session,
                    context,
                    run=run,
                    execution_attempt=1,
                    candidate=RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": f"queue-cancel:{run.id}",
                            "event_type": "run_cancelled",
                            "occurred_at": now,
                            "payload_version": "1.0",
                            "payload": {
                                "reason": request.reason or "Cancelled while queued.",
                                "cancelled_by": str(user_id),
                                "forced": False,
                            },
                        }
                    ),
                )
            if (
                run.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}
                and run.status != "CANCELLING"
            ):
                ensure_run_transition(cast(RunStatus, run.status), "CANCELLING")
                run.status = "CANCELLING"
                run.cancelling_at = datetime.now(UTC)
                await session.flush()
            result = _run_record(run)
            await _audit(
                session,
                tenant_id=tenant_id,
                actor_id=user_id,
                run_id=run.id,
                action="run.cancel",
                metadata=metadata,
                change={
                    "status": result.status,
                    "reason_present": request.reason is not None,
                    "workflow_bound": run.workflow_id is not None,
                },
            )
            await complete_idempotency(
                session,
                idempotency_id,
                response_status=202,
                response_body=_run_json(result),
                response_etag=None,
                response_ref=str(run.id),
            )
            return MutationOutcome(value=result)

    async def retry_run(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        request: RetryRunRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord] | None:
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
                session = unit_of_work.session
                idempotency_id, replay = await claim_idempotency(
                    session,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    operation_type="run.retry",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return MutationOutcome(replay=replay)
                source = await session.scalar(
                    select(AgentRunModel)
                    .join(
                        ChatSessionModel,
                        and_(
                            ChatSessionModel.tenant_id == AgentRunModel.tenant_id,
                            ChatSessionModel.id == AgentRunModel.session_id,
                        ),
                    )
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == run_id,
                        ChatSessionModel.user_id == user_id,
                        ChatSessionModel.status != "DELETED",
                    )
                    .with_for_update()
                )
                if source is None:
                    return None
                if source.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}:
                    raise resource_state_conflict("Only a terminal Run can be retried.")
                chat_session = await session.scalar(
                    select(ChatSessionModel)
                    .where(
                        ChatSessionModel.tenant_id == tenant_id,
                        ChatSessionModel.id == source.session_id,
                        ChatSessionModel.user_id == user_id,
                    )
                    .with_for_update()
                )
                if chat_session is None:
                    return None
                if chat_session.status != "ACTIVE":
                    raise resource_state_conflict(
                        "Runs can only be retried in an active Session."
                    )
                expected_cursor = source.assistant_message_id or source.user_message_id
                if chat_session.cursor_message_id != expected_cursor:
                    raise resource_state_conflict(
                        "The Session has advanced; retry requires an explicit branch."
                    )
                active_run = await session.scalar(
                    select(AgentRunModel.id).where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.session_id == source.session_id,
                        AgentRunModel.branch_id.is_not_distinct_from(source.branch_id),
                        AgentRunModel.status.in_(NON_TERMINAL_RUN_STATUSES),
                    )
                )
                if active_run is not None:
                    raise run_already_active()
                deployment = await self._retry_deployment(
                    session,
                    tenant_id=tenant_id,
                    source=source,
                    policy=request.deployment_policy,
                )
                message = await session.scalar(
                    select(ChatMessageModel).where(
                        ChatMessageModel.tenant_id == tenant_id,
                        ChatMessageModel.id == source.user_message_id,
                        ChatMessageModel.session_id == source.session_id,
                        ChatMessageModel.role == "USER",
                        ChatMessageModel.source_run_id.is_(None),
                    )
                )
                if message is None:
                    raise resource_state_conflict(
                        "The original immutable Run input is unavailable."
                    )
                now = datetime.now(UTC)
                run = AgentRunModel(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    session_id=source.session_id,
                    branch_id=source.branch_id,
                    user_message_id=source.user_message_id,
                    assistant_message_id=None,
                    agent_id=source.agent_id,
                    snapshot_id=deployment.snapshot_id,
                    deployment_id=deployment.id,
                    status="QUEUED",
                    current_attempt=0,
                    latest_sequence_no=0,
                    idempotency_key=idempotency_key,
                    client_request_id=None,
                    retry_of_run_id=source.id,
                    timeout_seconds=source.timeout_seconds,
                    token_budget=source.token_budget,
                    cost_budget_amount=source.cost_budget_amount,
                    cost_budget_currency=source.cost_budget_currency,
                    created_by=user_id,
                    created_at=now,
                    queued_at=now,
                )
                session.add(run)
                await session.flush()
                await self._enqueue_run(
                    session,
                    tenant_id=tenant_id,
                    run=run,
                    now=now,
                )
                chat_session.cursor_message_id = source.user_message_id
                chat_session.updated_at = now
                chat_session.resource_version += 1
                result = _run_record(run)
                await _audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    run_id=run.id,
                    action="run.retry",
                    metadata=metadata,
                    change={
                        "retry_of_run_id": str(source.id),
                        "deployment_policy": request.deployment_policy,
                        "deployment_id": str(deployment.id),
                        "snapshot_id": str(deployment.snapshot_id),
                        "reused_user_message_id": str(source.user_message_id),
                        "queue_priority": "NORMAL",
                        "queue_deadline_at": (now + self._queue_max_wait).isoformat(),
                    },
                )
                await complete_idempotency(
                    session,
                    idempotency_id,
                    response_status=202,
                    response_body=_accepted_json(result),
                    response_etag=None,
                    response_ref=str(run.id),
                )
                return MutationOutcome(value=result)
        except IntegrityError as exc:
            constraint = getattr(
                getattr(exc.orig, "diag", None), "constraint_name", None
            )
            if constraint == "uq_agent_run__active_session_branch":
                raise run_already_active() from exc
            raise

    async def load_run_spec_source(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunSpecCompilationSource | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            row = (
                await unit_of_work.session.execute(
                    select(
                        AgentRunModel,
                        ChatMessageModel,
                        DeploymentModel,
                        RuntimeBundleModel,
                        AgentSnapshotModel,
                        AgentVersionModel,
                    )
                    .join(
                        ChatMessageModel,
                        and_(
                            ChatMessageModel.tenant_id == AgentRunModel.tenant_id,
                            ChatMessageModel.id == AgentRunModel.user_message_id,
                            ChatMessageModel.session_id == AgentRunModel.session_id,
                        ),
                    )
                    .join(
                        DeploymentModel,
                        and_(
                            DeploymentModel.tenant_id == AgentRunModel.tenant_id,
                            DeploymentModel.id == AgentRunModel.deployment_id,
                            DeploymentModel.snapshot_id == AgentRunModel.snapshot_id,
                        ),
                    )
                    .join(
                        RuntimeBundleModel,
                        and_(
                            RuntimeBundleModel.tenant_id == DeploymentModel.tenant_id,
                            RuntimeBundleModel.id == DeploymentModel.bundle_id,
                            RuntimeBundleModel.snapshot_id
                            == DeploymentModel.snapshot_id,
                        ),
                    )
                    .join(
                        AgentSnapshotModel,
                        and_(
                            AgentSnapshotModel.tenant_id == AgentRunModel.tenant_id,
                            AgentSnapshotModel.id == AgentRunModel.snapshot_id,
                        ),
                    )
                    .join(
                        AgentVersionModel,
                        and_(
                            AgentVersionModel.tenant_id == AgentSnapshotModel.tenant_id,
                            AgentVersionModel.id == AgentSnapshotModel.agent_version_id,
                        ),
                    )
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == run_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            run, message, deployment, bundle, snapshot, version = row
            if message.role != "USER" or message.source_run_id is not None:
                raise resource_state_conflict("The Run user Message is invalid.")
            content = parse_message_content_parts(message.content_parts_json)
            if len(content) != 1 or content[0].type != "text":
                raise resource_state_conflict("The Run input Message is invalid.")
            if bundle.scan_status != "PASSED":
                raise resource_state_conflict(
                    "The Run Runtime Bundle has not passed security scanning."
                )
            try:
                admit_runtime_bundle(
                    RuntimeBundleAdmissionFacts(
                        compiler_name=bundle.compiler_name,
                        compiler_version=bundle.compiler_version,
                        scan_status=bundle.scan_status,
                        manifest=bundle.manifest_json,
                    )
                )
            except AdmissionDenied as error:
                raise resource_state_conflict(f"{error.code}: {error}") from error
            if bundle.runtime_type not in {"agentscope", "codex"}:
                raise resource_state_conflict("The Run Runtime type is unsupported.")
            user_text = content[0].text
            if user_text is None:
                raise resource_state_conflict("The Run input Message is invalid.")
            return RunSpecCompilationSource(
                tenant_id=run.tenant_id,
                run_id=run.id,
                user_id=run.created_by,
                session_id=run.session_id,
                branch_id=run.branch_id,
                user_message_id=run.user_message_id,
                user_text=user_text,
                agent_id=run.agent_id,
                agent_version_id=version.id,
                snapshot_id=run.snapshot_id,
                snapshot_content=cast(dict[str, JsonValue], snapshot.content_json),
                deployment_id=run.deployment_id,
                bundle_id=bundle.id,
                bundle_uri=bundle.object_uri,
                bundle_hash=bundle.content_hash,
                bundle_size_bytes=bundle.size_bytes,
                bundle_compiler_version=bundle.compiler_version,
                runtime_type=cast(Literal["agentscope", "codex"], bundle.runtime_type),
                runtime_target_id=deployment.runtime_target_id,
                timeout_seconds=run.timeout_seconds,
                token_budget=run.token_budget,
                cost_budget_amount=run.cost_budget_amount,
                cost_budget_currency=run.cost_budget_currency,
                idempotency_key=run.idempotency_key,
                status=cast(RunStatus, run.status),
                current_attempt=run.current_attempt,
                workflow_id=run.workflow_id,
            )

    async def prepare_run(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        workflow_id: str,
        execution_attempt: int,
        fencing_token_hash: str,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise resource_state_conflict("The Run is unavailable.")
            attempt = await session.scalar(
                select(RunAttemptModel).where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == execution_attempt,
                )
            )
            if run.status in {"PREPARING", "RUNNING"}:
                if (
                    run.workflow_id != workflow_id
                    or run.current_attempt != execution_attempt
                    or attempt is None
                    or attempt.fencing_token_hash != fencing_token_hash
                ):
                    raise resource_state_conflict(
                        "The Run preparation replay does not match persisted facts."
                    )
                return
            if run.status == "CANCELLING":
                raise RunStageError(
                    "RUN_CANCELLING", "The Run cancellation is already pending."
                )
            if run.status != "QUEUED" or run.current_attempt != 0:
                raise resource_state_conflict(
                    "The Run cannot be prepared from its current state."
                )
            queue = await session.scalar(
                select(RunAdmissionQueueModel).where(
                    RunAdmissionQueueModel.tenant_id == tenant_id,
                    RunAdmissionQueueModel.run_id == run_id,
                    RunAdmissionQueueModel.status == "ADMITTED",
                )
            )
            if queue is None:
                raise resource_state_conflict(
                    "The Run does not have a durable admission decision."
                )
            if attempt is not None or execution_attempt != 1:
                raise resource_state_conflict("The Run attempt allocation is invalid.")
            if run.workflow_id is not None and run.workflow_id != workflow_id:
                raise resource_state_conflict(
                    "The Run is already bound to another Workflow."
                )
            run.workflow_id = workflow_id
            attempt = RunAttemptModel(
                id=uuid5(
                    NAMESPACE_URL,
                    f"run-attempt/{tenant_id}/{run_id}/{execution_attempt}",
                ),
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_no=execution_attempt,
                fencing_token_hash=fencing_token_hash,
                worker_id=None,
                runtime_handle_ref=None,
                status="ALLOCATED",
                started_at=None,
                heartbeat_at=None,
                finished_at=None,
                error_code=None,
            )
            session.add(attempt)
            run.current_attempt = execution_attempt
            await session.flush()
            ensure_run_transition("QUEUED", "PREPARING")
            run.status = "PREPARING"
            await session.flush()

    async def mark_run_running(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        worker_id: str,
        fencing_token_hash: str | None = None,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                )
                .with_for_update()
            )
            attempt = await session.scalar(
                select(RunAttemptModel)
                .where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == execution_attempt,
                )
                .with_for_update()
            )
            if run is None or attempt is None:
                raise resource_state_conflict("The Run attempt is unavailable.")
            if run.status == "CANCELLING":
                raise RunStageError(
                    "RUN_CANCELLING", "The Run cancellation is already pending."
                )
            if (
                fencing_token_hash is not None
                and attempt.fencing_token_hash != fencing_token_hash
            ):
                raise resource_state_conflict("The Run fencing token is stale.")
            if run.status == "RUNNING" and attempt.status == "RUNNING":
                return
            if (
                run.current_attempt != execution_attempt
                or attempt.status != "ALLOCATED"
            ):
                raise resource_state_conflict(
                    "The Run attempt cannot enter execution from its current state."
                )
            if run.status not in {"PREPARING", "RUNNING"}:
                raise resource_state_conflict(
                    "The Run attempt cannot enter execution from its current state."
                )
            now = datetime.now(UTC)
            ensure_run_attempt_transition("ALLOCATED", "STARTING")
            attempt.status = "STARTING"
            attempt.worker_id = worker_id
            attempt.started_at = now
            await session.flush()
            ensure_run_attempt_transition("STARTING", "RUNNING")
            attempt.status = "RUNNING"
            attempt.heartbeat_at = now
            await session.flush()
            if run.status == "PREPARING":
                ensure_run_transition("PREPARING", "RUNNING")
                run.status = "RUNNING"
            if run.started_at is None:
                run.started_at = now
            await session.flush()

    async def load_run_attempt(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
    ) -> RunAttemptRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit_of_work:
            attempt = await unit_of_work.session.scalar(
                select(RunAttemptModel).where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == execution_attempt,
                )
            )
            return _attempt_record(attempt) if attempt is not None else None

    async def prepare_recovery_attempt(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        lost_execution_attempt: int,
        execution_attempt: int,
        fencing_token_hash: str,
    ) -> None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                )
                .with_for_update()
            )
            previous = await session.scalar(
                select(RunAttemptModel)
                .where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == lost_execution_attempt,
                )
                .with_for_update()
            )
            current = await session.scalar(
                select(RunAttemptModel).where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == execution_attempt,
                )
            )
            if run is None or previous is None:
                raise resource_state_conflict("The Run attempt is unavailable.")
            if current is not None:
                if (
                    run.status == "RUNNING"
                    and run.current_attempt == execution_attempt
                    and current.status in {"ALLOCATED", "STARTING", "RUNNING"}
                    and current.fencing_token_hash == fencing_token_hash
                    and previous.status == "LOST"
                ):
                    return
                raise resource_state_conflict(
                    "The recovery attempt conflicts with persisted facts."
                )
            if (
                run.status != "RUNNING"
                or run.current_attempt != lost_execution_attempt
                or execution_attempt != lost_execution_attempt + 1
                or previous.status not in {"ALLOCATED", "STARTING", "RUNNING"}
            ):
                raise resource_state_conflict(
                    "The Run cannot allocate a recovery attempt."
                )
            now = datetime.now(UTC)
            ensure_run_attempt_transition(
                cast(Literal["ALLOCATED", "STARTING", "RUNNING"], previous.status),
                "LOST",
            )
            previous.status = "LOST"
            previous.error_code = "RUNTIME_LOST"
            if previous.started_at is not None:
                previous.heartbeat_at = now
                previous.finished_at = now
            replacement = RunAttemptModel(
                id=uuid5(
                    NAMESPACE_URL,
                    f"run-attempt/{tenant_id}/{run_id}/{execution_attempt}",
                ),
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_no=execution_attempt,
                fencing_token_hash=fencing_token_hash,
                worker_id=None,
                runtime_handle_ref=None,
                status="ALLOCATED",
                started_at=None,
                heartbeat_at=None,
                finished_at=None,
                error_code=None,
            )
            session.add(replacement)
            run.current_attempt = execution_attempt
            await session.flush()

    async def finalize_run(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunInput,
        fencing_token_hash: str | None = None,
    ) -> FinalizeAgentRunResult:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == input.run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise resource_state_conflict("The Run is unavailable.")
            if run.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}:
                return FinalizeAgentRunResult(
                    run_id=run.id,
                    status=cast(
                        Literal["SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"],
                        run.status,
                    ),
                    assistant_message_id=run.assistant_message_id,
                )
            if run.status == "QUEUED" and run.workflow_id is None:
                queue = await session.scalar(
                    select(RunAdmissionQueueModel)
                    .where(
                        RunAdmissionQueueModel.tenant_id == tenant_id,
                        RunAdmissionQueueModel.run_id == run.id,
                    )
                    .with_for_update()
                )
                if queue is not None and queue.status == "WAITING":
                    now = datetime.now(UTC)
                    queue.status = "CANCELLED"
                    queue.cancelled_at = now
                    queue.updated_at = now
                    queue.resource_version += 1
                    ensure_run_transition("QUEUED", "CANCELLED")
                    run.status = "CANCELLED"
                    run.finished_at = now
                    run.error_code = None
                    run.error_detail_json = None
                    await session.flush()
                    return FinalizeAgentRunResult(
                        run_id=run.id,
                        status="CANCELLED",
                        assistant_message_id=None,
                    )
            attempt = None
            if input.execution_attempt > 0:
                attempt = await session.scalar(
                    select(RunAttemptModel)
                    .where(
                        RunAttemptModel.tenant_id == tenant_id,
                        RunAttemptModel.run_id == input.run_id,
                        RunAttemptModel.attempt_no == input.execution_attempt,
                    )
                    .with_for_update()
                )
                if (
                    attempt is not None
                    and fencing_token_hash is not None
                    and attempt.fencing_token_hash != fencing_token_hash
                ):
                    raise resource_state_conflict("The Run fencing token is stale.")
            now = datetime.now(UTC)
            if input.completion.status == "SUCCEEDED":
                if (
                    run.status != "RUNNING"
                    or run.current_attempt != input.execution_attempt
                    or attempt is None
                    or attempt.status != "RUNNING"
                ):
                    raise resource_state_conflict(
                        "The Run cannot be completed from its current state."
                    )
                content_parts = [
                    part.model_dump(mode="json", exclude_none=True)
                    for part in input.completion.assistant_content_parts
                ]
                if any(
                    part["type"] in {"artifact_reference", "tool_reference"}
                    for part in content_parts
                ):
                    raise resource_state_conflict(
                        "Governed Artifact and Tool facts are required before linking them."
                    )
                parse_message_content_parts(content_parts)
                chat_session = await session.scalar(
                    select(ChatSessionModel)
                    .where(
                        ChatSessionModel.tenant_id == tenant_id,
                        ChatSessionModel.id == run.session_id,
                    )
                    .with_for_update()
                )
                if (
                    chat_session is None
                    or chat_session.cursor_message_id != run.user_message_id
                ):
                    raise resource_state_conflict(
                        "The Session cursor no longer matches the Run input."
                    )
                assistant = ChatMessageModel(
                    tenant_id=tenant_id,
                    session_id=run.session_id,
                    branch_id=run.branch_id,
                    parent_message_id=run.user_message_id,
                    role="ASSISTANT",
                    content_parts_json=content_parts,
                    source_run_id=run.id,
                    created_at=now,
                    created_by=run.created_by,
                )
                session.add(assistant)
                await session.flush()
                ensure_run_attempt_transition("RUNNING", "COMPLETED")
                attempt.status = "COMPLETED"
                attempt.runtime_handle_ref = input.completion.runtime_handle_ref
                attempt.heartbeat_at = now
                attempt.finished_at = now
                await session.flush()
                ensure_run_transition("RUNNING", "SUCCEEDED")
                run.assistant_message_id = assistant.id
                run.status = "SUCCEEDED"
                run.result_quality = input.completion.result_quality or "NORMAL"
                run.finished_at = now
                chat_session.cursor_message_id = assistant.id
                chat_session.updated_at = now
                chat_session.resource_version += 1
                await session.flush()
                return FinalizeAgentRunResult(
                    run_id=run.id,
                    status="SUCCEEDED",
                    assistant_message_id=assistant.id,
                )

            current_status = cast(RunStatus, run.status)
            ensure_run_transition(current_status, "FAILED")
            run.status = "FAILED"
            run.result_quality = None
            run.error_code = input.completion.error_code
            run.error_detail_json = {
                "message": input.completion.error_message
                or "The Run execution failed.",
                "request_id": input.request_id,
                "retryable": input.completion.retryable,
            }
            run.finished_at = now
            if attempt is not None:
                if run.current_attempt != input.execution_attempt:
                    raise resource_state_conflict(
                        "The Run failure attempt does not match current execution."
                    )
                attempt_status = cast(
                    Literal["ALLOCATED", "STARTING", "RUNNING"], attempt.status
                )
                ensure_run_attempt_transition(attempt_status, "LOST")
                attempt.status = "LOST"
                attempt.error_code = input.completion.error_code
                attempt.runtime_handle_ref = input.completion.runtime_handle_ref
                if attempt.started_at is not None:
                    attempt.heartbeat_at = now
                    attempt.finished_at = now
            await session.flush()
            return FinalizeAgentRunResult(
                run_id=run.id,
                status="FAILED",
                assistant_message_id=None,
            )

    async def finalize_cancellation(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunCancellationInput,
        fencing_token_hash: str | None,
    ) -> FinalizeAgentRunResult:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == input.run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise resource_state_conflict("The Run is unavailable.")
            if run.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}:
                return FinalizeAgentRunResult(
                    run_id=run.id,
                    status=cast(
                        Literal["SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"],
                        run.status,
                    ),
                    assistant_message_id=run.assistant_message_id,
                )
            attempt = None
            execution_attempt = input.execution_attempt or run.current_attempt
            if execution_attempt > 0:
                attempt = await session.scalar(
                    select(RunAttemptModel)
                    .where(
                        RunAttemptModel.tenant_id == tenant_id,
                        RunAttemptModel.run_id == input.run_id,
                        RunAttemptModel.attempt_no == execution_attempt,
                    )
                    .with_for_update()
                )
                if (
                    attempt is None
                    or attempt.fencing_token_hash != fencing_token_hash
                    or run.current_attempt != execution_attempt
                ):
                    raise resource_state_conflict("The Run fencing token is stale.")
            if run.status != "CANCELLING":
                raise resource_state_conflict(
                    "The Run cancellation cannot be finalized from its current state."
                )
            now = datetime.now(UTC)
            if input.cancellation.status in {"CANCELLED", "ALREADY_STOPPED"}:
                if attempt is not None and attempt.status in {
                    "ALLOCATED",
                    "STARTING",
                    "RUNNING",
                }:
                    ensure_run_attempt_transition(
                        cast(
                            Literal["ALLOCATED", "STARTING", "RUNNING"], attempt.status
                        ),
                        "CANCELLED",
                    )
                    attempt.status = "CANCELLED"
                    if attempt.started_at is not None:
                        attempt.finished_at = now
                        attempt.heartbeat_at = now
                ensure_run_transition("CANCELLING", "CANCELLED")
                run.status = "CANCELLED"
                run.error_code = None
                run.error_detail_json = None
            else:
                ensure_run_transition("CANCELLING", "FAILED")
                run.status = "FAILED"
                run.error_code = (
                    input.cancellation.error_code or "CANCEL_STATUS_UNKNOWN"
                )
                run.error_detail_json = {
                    "message": "The Runtime cancellation status is unknown.",
                    "request_id": input.request_id,
                    "retryable": False,
                }
                if attempt is not None and attempt.status in {
                    "ALLOCATED",
                    "STARTING",
                    "RUNNING",
                }:
                    ensure_run_attempt_transition(
                        cast(
                            Literal["ALLOCATED", "STARTING", "RUNNING"], attempt.status
                        ),
                        "LOST",
                    )
                    attempt.status = "LOST"
                    attempt.error_code = run.error_code
                    if attempt.started_at is not None:
                        attempt.finished_at = now
                        attempt.heartbeat_at = now
            run.finished_at = now
            await session.flush()
            return FinalizeAgentRunResult(
                run_id=run.id,
                status=cast(
                    Literal["SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"],
                    run.status,
                ),
                assistant_message_id=run.assistant_message_id,
            )

    async def _deployment(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        chat_session: ChatSessionModel,
        requested_id: str | None,
    ) -> DeploymentModel:
        deployment_id = (
            _resource_id(requested_id)
            if requested_id is not None
            else chat_session.default_deployment_id
        )
        allowed_statuses = (
            ("ACTIVE", "DEGRADED")
            if requested_id is not None
            else ("ACTIVE", "DEGRADED", "RETIRED")
        )
        deployment = await session.scalar(
            select(DeploymentModel).where(
                DeploymentModel.tenant_id == tenant_id,
                DeploymentModel.id == deployment_id,
                DeploymentModel.agent_id == chat_session.agent_id,
                DeploymentModel.status.in_(allowed_statuses),
            )
        )
        if deployment is None:
            raise resource_state_conflict(
                "The requested Deployment is unavailable for this Session Agent."
            )
        await self._admit_deployment(
            session, tenant_id=tenant_id, deployment=deployment
        )
        return deployment

    async def _retry_deployment(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        source: AgentRunModel,
        policy: Literal["original_snapshot", "current_deployment"],
    ) -> DeploymentModel:
        if policy == "original_snapshot":
            deployment = await session.scalar(
                select(DeploymentModel).where(
                    DeploymentModel.tenant_id == tenant_id,
                    DeploymentModel.id == source.deployment_id,
                    DeploymentModel.agent_id == source.agent_id,
                    DeploymentModel.snapshot_id == source.snapshot_id,
                    DeploymentModel.status.in_(("ACTIVE", "DEGRADED", "RETIRED")),
                )
            )
        else:
            agent = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == source.agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
            )
            deployment = (
                None
                if agent is None or agent.active_deployment_id is None
                else await session.scalar(
                    select(DeploymentModel).where(
                        DeploymentModel.tenant_id == tenant_id,
                        DeploymentModel.id == agent.active_deployment_id,
                        DeploymentModel.agent_id == source.agent_id,
                        DeploymentModel.status.in_(("ACTIVE", "DEGRADED")),
                    )
                )
            )
        if deployment is None:
            raise resource_state_conflict(
                "The requested retry Deployment is unavailable."
            )
        await self._admit_deployment(
            session, tenant_id=tenant_id, deployment=deployment
        )
        return deployment

    async def _admit_deployment(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        deployment: DeploymentModel,
    ) -> None:
        bundle = await session.scalar(
            select(RuntimeBundleModel).where(
                RuntimeBundleModel.tenant_id == tenant_id,
                RuntimeBundleModel.id == deployment.bundle_id,
                RuntimeBundleModel.snapshot_id == deployment.snapshot_id,
            )
        )
        if bundle is None:
            raise resource_state_conflict(
                "The Deployment Runtime Bundle is unavailable for admission."
            )
        try:
            admit_runtime_bundle(
                RuntimeBundleAdmissionFacts(
                    compiler_name=bundle.compiler_name,
                    compiler_version=bundle.compiler_version,
                    scan_status=bundle.scan_status,
                    manifest=bundle.manifest_json,
                )
            )
        except AdmissionDenied as error:
            raise resource_state_conflict(f"{error.code}: {error}") from error


async def _admit_run_capacity(
    session: AsyncSession,
    *,
    policy: RunCapacityPolicy,
    tenant_id: UUID,
    user_id: UUID,
    agent_id: UUID,
    deployment: DeploymentModel,
) -> tuple[UUID | None, RunCapacityPolicy, Literal["agentscope", "codex"]]:
    tenant_policy_version_id, tenant_policy = (
        await load_active_run_capacity_policy_version(session, tenant_id)
    )
    effective_policy = (
        policy.narrowed_by(tenant_policy) if tenant_policy is not None else policy
    )
    if not effective_policy.enabled:
        runtime_type = await _deployment_runtime_type(
            session, tenant_id=tenant_id, deployment=deployment
        )
        return tenant_policy_version_id, effective_policy, runtime_type
    runtime_type = await _deployment_runtime_type(
        session, tenant_id=tenant_id, deployment=deployment
    )
    lock_keys: list[str] = []
    if effective_policy.max_nonterminal_runs_per_tenant is not None:
        lock_keys.append(f"run-capacity:tenant:{tenant_id}")
    if effective_policy.max_nonterminal_runs_per_user is not None:
        lock_keys.append(f"run-capacity:user:{tenant_id}:{user_id}")
    if effective_policy.max_nonterminal_runs_per_agent is not None:
        lock_keys.append(f"run-capacity:agent:{tenant_id}:{agent_id}")
    if effective_policy.runtime_limit(runtime_type) is not None:
        lock_keys.append(f"run-capacity:runtime:{tenant_id}:{runtime_type}")
    for lock_key in sorted(lock_keys):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    base = (
        AgentRunModel.tenant_id == tenant_id,
        AgentRunModel.status.in_(EXECUTION_CAPACITY_RUN_STATUSES),
    )
    tenant_count = await session.scalar(
        select(func.count(AgentRunModel.id)).where(*base)
    )
    admitted_base = (
        RunAdmissionQueueModel.tenant_id == tenant_id,
        RunAdmissionQueueModel.status == "ADMITTED",
        AgentRunModel.status == "QUEUED",
    )
    admitted_tenant_count = await session.scalar(
        select(func.count(RunAdmissionQueueModel.id))
        .select_from(RunAdmissionQueueModel)
        .join(
            AgentRunModel,
            and_(
                AgentRunModel.tenant_id == RunAdmissionQueueModel.tenant_id,
                AgentRunModel.id == RunAdmissionQueueModel.run_id,
            ),
        )
        .where(*admitted_base)
    )
    user_count = await session.scalar(
        select(func.count(AgentRunModel.id)).where(
            *base, AgentRunModel.created_by == user_id
        )
    )
    admitted_user_count = await session.scalar(
        select(func.count(RunAdmissionQueueModel.id))
        .select_from(RunAdmissionQueueModel)
        .join(
            AgentRunModel,
            and_(
                AgentRunModel.tenant_id == RunAdmissionQueueModel.tenant_id,
                AgentRunModel.id == RunAdmissionQueueModel.run_id,
            ),
        )
        .where(*admitted_base, AgentRunModel.created_by == user_id)
    )
    agent_count = await session.scalar(
        select(func.count(AgentRunModel.id)).where(
            *base, AgentRunModel.agent_id == agent_id
        )
    )
    admitted_agent_count = await session.scalar(
        select(func.count(RunAdmissionQueueModel.id))
        .select_from(RunAdmissionQueueModel)
        .join(
            AgentRunModel,
            and_(
                AgentRunModel.tenant_id == RunAdmissionQueueModel.tenant_id,
                AgentRunModel.id == RunAdmissionQueueModel.run_id,
            ),
        )
        .where(*admitted_base, AgentRunModel.agent_id == agent_id)
    )
    runtime_count = await session.scalar(
        select(func.count(AgentRunModel.id))
        .select_from(AgentRunModel)
        .join(
            DeploymentModel,
            and_(
                DeploymentModel.tenant_id == AgentRunModel.tenant_id,
                DeploymentModel.id == AgentRunModel.deployment_id,
            ),
        )
        .join(
            RuntimeBundleModel,
            and_(
                RuntimeBundleModel.tenant_id == DeploymentModel.tenant_id,
                RuntimeBundleModel.id == DeploymentModel.bundle_id,
                RuntimeBundleModel.snapshot_id == DeploymentModel.snapshot_id,
            ),
        )
        .where(*base, RuntimeBundleModel.runtime_type == runtime_type)
    )
    admitted_runtime_count = await session.scalar(
        select(func.count(RunAdmissionQueueModel.id))
        .select_from(RunAdmissionQueueModel)
        .join(
            AgentRunModel,
            and_(
                AgentRunModel.tenant_id == RunAdmissionQueueModel.tenant_id,
                AgentRunModel.id == RunAdmissionQueueModel.run_id,
            ),
        )
        .join(
            DeploymentModel,
            and_(
                DeploymentModel.tenant_id == AgentRunModel.tenant_id,
                DeploymentModel.id == AgentRunModel.deployment_id,
            ),
        )
        .join(
            RuntimeBundleModel,
            and_(
                RuntimeBundleModel.tenant_id == DeploymentModel.tenant_id,
                RuntimeBundleModel.id == DeploymentModel.bundle_id,
                RuntimeBundleModel.snapshot_id == DeploymentModel.snapshot_id,
            ),
        )
        .where(*admitted_base, RuntimeBundleModel.runtime_type == runtime_type)
    )
    admit_run_capacity(
        effective_policy,
        RunCapacityFacts(
            tenant_nonterminal_runs=int(tenant_count or 0)
            + int(admitted_tenant_count or 0),
            user_nonterminal_runs=int(user_count or 0) + int(admitted_user_count or 0),
            agent_nonterminal_runs=int(agent_count or 0)
            + int(admitted_agent_count or 0),
            runtime_nonterminal_runs=int(runtime_count or 0)
            + int(admitted_runtime_count or 0),
            runtime_type=runtime_type,
        ),
    )
    return tenant_policy_version_id, effective_policy, runtime_type


async def _deployment_runtime_type(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    deployment: DeploymentModel,
) -> Literal["agentscope", "codex"]:
    runtime_type_value = await session.scalar(
        select(RuntimeBundleModel.runtime_type).where(
            RuntimeBundleModel.tenant_id == tenant_id,
            RuntimeBundleModel.id == deployment.bundle_id,
            RuntimeBundleModel.snapshot_id == deployment.snapshot_id,
        )
    )
    if runtime_type_value not in {"agentscope", "codex"}:
        raise resource_state_conflict(
            "The Deployment Runtime type is unavailable for capacity admission."
        )
    return cast(Literal["agentscope", "codex"], runtime_type_value)


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _capacity_snapshot(
    policy: RunCapacityPolicy,
    runtime_type: Literal["agentscope", "codex"],
) -> dict[str, JsonValue]:
    return {
        "runtime_type": runtime_type,
        "max_nonterminal_runs_per_tenant": policy.max_nonterminal_runs_per_tenant,
        "max_nonterminal_runs_per_user": policy.max_nonterminal_runs_per_user,
        "max_nonterminal_runs_per_agent": policy.max_nonterminal_runs_per_agent,
        "max_nonterminal_agentscope_runs": policy.max_nonterminal_agentscope_runs,
        "max_nonterminal_codex_runs": policy.max_nonterminal_codex_runs,
    }


def _capacity_config_hash(domain_key: str, slots: int) -> str:
    encoded = json.dumps(
        {"domain_key": domain_key, "configured_slots": slots},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


async def _sync_capacity_domains(
    session: AsyncSession,
    *,
    configured: Mapping[str, int],
    now: datetime,
) -> None:
    existing = {
        model.domain_key: model
        for model in await session.scalars(
            select(RunCapacityDomainModel).with_for_update()
        )
    }
    for domain_key, slots in sorted(configured.items()):
        config_hash = _capacity_config_hash(domain_key, slots)
        model = existing.pop(domain_key, None)
        if model is None:
            session.add(
                RunCapacityDomainModel(
                    domain_key=domain_key,
                    configured_slots=slots,
                    status="ACTIVE",
                    config_hash=config_hash,
                    resource_version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            continue
        if (
            model.configured_slots != slots
            or model.status != "ACTIVE"
            or model.config_hash != config_hash
        ):
            model.configured_slots = slots
            model.status = "ACTIVE"
            model.config_hash = config_hash
            model.updated_at = now
            model.resource_version += 1
    for model in existing.values():
        if model.status == "ACTIVE":
            model.status = "DRAINING"
            model.updated_at = now
            model.resource_version += 1
    await session.flush()


async def _reconcile_capacity_leases(
    session: AsyncSession,
    *,
    now: datetime,
    lease_ttl: timedelta,
    limit: int,
) -> tuple[int, int]:
    leases = list(
        await session.scalars(
            select(RunCapacityLeaseModel)
            .where(RunCapacityLeaseModel.released_at.is_(None))
            .order_by(RunCapacityLeaseModel.expires_at, RunCapacityLeaseModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    released = renewed = 0
    for lease in leases:
        await _bind_scheduler_tenant(session, lease.tenant_id)
        run = await session.scalar(
            select(AgentRunModel).where(
                AgentRunModel.tenant_id == lease.tenant_id,
                AgentRunModel.id == lease.run_id,
            )
        )
        if run is None:
            lease.released_at = now
            lease.release_reason = "ORPHANED"
            lease.resource_version += 1
            released += 1
            continue
        if run.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}:
            lease.released_at = now
            lease.release_reason = "RUN_TERMINAL"
            lease.resource_version += 1
            released += 1
            continue
        if lease.expires_at <= now:
            lease.renewed_at = now
            lease.expires_at = now + lease_ttl
            lease.resource_version += 1
            renewed += 1
    await session.flush()
    return released, renewed


async def _expire_capacity_queue_entries(
    session: AsyncSession,
    *,
    scheduler_context: TenantContext,
    now: datetime,
    limit: int,
) -> int:
    queues = list(
        await session.scalars(
            select(RunAdmissionQueueModel)
            .where(
                RunAdmissionQueueModel.status == "WAITING",
                RunAdmissionQueueModel.deadline_at <= now,
            )
            .order_by(
                RunAdmissionQueueModel.deadline_at,
                RunAdmissionQueueModel.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    expired = 0
    for queue in queues:
        await _bind_scheduler_tenant(session, queue.tenant_id)
        context = _scheduler_tenant_context(
            scheduler_context, tenant_id=queue.tenant_id
        )
        run = await session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.tenant_id == queue.tenant_id,
                AgentRunModel.id == queue.run_id,
            )
            .with_for_update()
        )
        if run is None or run.status != "QUEUED":
            continue
        queue.status = "TIMED_OUT"
        queue.cancelled_at = now
        queue.updated_at = now
        queue.resource_version += 1
        ensure_run_transition("QUEUED", "TIMEOUT")
        run.status = "TIMEOUT"
        run.error_code = "RUN_QUEUE_TIMEOUT"
        run.error_detail_json = {
            "message": "The Run exceeded its admission queue deadline.",
            "request_id": context.request_id,
            "retryable": True,
            "stage": "queue",
        }
        run.finished_at = now
        await append_control_run_event(
            session,
            context,
            run=run,
            execution_attempt=1,
            candidate=RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                {
                    "source_event_id": f"queue-timeout:{run.id}",
                    "event_type": "run_timeout",
                    "occurred_at": now,
                    "payload_version": "1.0",
                    "payload": {
                        "timeout_seconds": max(
                            1,
                            int((queue.deadline_at - queue.queued_at).total_seconds()),
                        ),
                        "stage": "queue",
                    },
                }
            ),
        )
        _reconciliation_audit(
            session,
            context=context,
            run_id=run.id,
            action="run.queue.timed_out",
            change={"deadline_at": queue.deadline_at.isoformat()},
            occurred_at=now,
        )
        expired += 1
    await session.flush()
    return expired


async def _bind_scheduler_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def _scheduler_tenant_context(
    scheduler_context: TenantContext, *, tenant_id: UUID
) -> TenantContext:
    return scheduler_context.model_copy(update={"tenant_id": str(tenant_id)})


def _fair_capacity_candidates(
    candidates: list[RunAdmissionQueueModel],
    *,
    last_admitted: Mapping[tuple[str, UUID], datetime | None],
    tenant_quantum: int,
) -> list[RunAdmissionQueueModel]:
    groups: dict[tuple[str, UUID], list[RunAdmissionQueueModel]] = {}
    for candidate in candidates:
        groups.setdefault((candidate.capacity_domain, candidate.tenant_id), []).append(
            candidate
        )
    ordered: list[RunAdmissionQueueModel] = []
    by_domain: dict[str, list[tuple[str, UUID]]] = {}
    for key in groups:
        by_domain.setdefault(key[0], []).append(key)
    for domain_key in sorted(by_domain):
        keys = sorted(
            by_domain[domain_key],
            key=lambda key: (
                last_admitted.get(key) or datetime.min.replace(tzinfo=UTC),
                groups[key][0].queued_at,
                str(key[1]),
            ),
        )
        while keys:
            remaining_keys: list[tuple[str, UUID]] = []
            for key in keys:
                group = groups[key]
                ordered.extend(group[:tenant_quantum])
                del group[:tenant_quantum]
                if group:
                    remaining_keys.append(key)
            keys = remaining_keys
    return ordered


def _ranked_capacity_candidates(*, now: datetime, capacity_domains: tuple[str, ...]):
    """Rank each domain/tenant backlog before the scheduler applies its global bound."""

    return (
        select(
            RunAdmissionQueueModel.id.label("queue_id"),
            func.row_number()
            .over(
                partition_by=(
                    RunAdmissionQueueModel.capacity_domain,
                    RunAdmissionQueueModel.tenant_id,
                ),
                order_by=(
                    (RunAdmissionQueueModel.priority == "HIGH").desc(),
                    RunAdmissionQueueModel.queued_at,
                    RunAdmissionQueueModel.id,
                ),
            )
            .label("tenant_position"),
        )
        .where(
            RunAdmissionQueueModel.status == "WAITING",
            RunAdmissionQueueModel.deadline_at > now,
            RunAdmissionQueueModel.capacity_domain.in_(capacity_domains),
        )
        .subquery()
    )


def _run_record(model: AgentRunModel) -> RunRecord:
    return RunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        branch_id=model.branch_id,
        user_message_id=model.user_message_id,
        assistant_message_id=model.assistant_message_id,
        agent_id=model.agent_id,
        snapshot_id=model.snapshot_id,
        deployment_id=model.deployment_id,
        status=cast(RunStatus, model.status),
        result_quality=cast(
            Literal["NORMAL", "SUCCEEDED_WITH_WARNINGS"] | None,
            model.result_quality,
        ),
        current_attempt=model.current_attempt,
        latest_sequence_no=model.latest_sequence_no,
        idempotency_key=model.idempotency_key,
        client_request_id=model.client_request_id,
        retry_of_run_id=model.retry_of_run_id,
        timeout_seconds=model.timeout_seconds,
        token_budget=model.token_budget,
        cost_budget_amount=model.cost_budget_amount,
        cost_budget_currency=model.cost_budget_currency,
        workflow_id=model.workflow_id,
        error_code=model.error_code,
        error_detail=cast(dict[str, JsonValue] | None, model.error_detail_json),
        created_by=model.created_by,
        created_at=model.created_at,
        queued_at=model.queued_at,
        started_at=model.started_at,
        finished_at=model.finished_at,
    )


def _attempt_record(model: RunAttemptModel) -> RunAttemptRecord:
    return RunAttemptRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        attempt_no=model.attempt_no,
        fencing_token_hash=model.fencing_token_hash,
        worker_id=model.worker_id,
        runtime_handle_ref=model.runtime_handle_ref,
        status=cast(
            Literal[
                "ALLOCATED",
                "STARTING",
                "RUNNING",
                "COMPLETED",
                "LOST",
                "CANCELLED",
            ],
            model.status,
        ),
        started_at=model.started_at,
        heartbeat_at=model.heartbeat_at,
        finished_at=model.finished_at,
        error_code=model.error_code,
    )


def _accepted_json(record: RunRecord) -> dict[str, object]:
    return {
        "run_id": str(record.id),
        "session_id": str(record.session_id),
        "status": record.status,
        "events_url": f"/api/v1/runs/{record.id}/events",
        "stream_url": f"/api/v1/runs/{record.id}/events/stream",
    }


def _run_json(record: RunRecord) -> dict[str, object]:
    error = None
    if record.error_code is not None:
        details = record.error_detail or {}
        error = {
            "code": record.error_code,
            "message": str(details.get("message", "Run execution failed.")),
            "request_id": str(details.get("request_id", "unknown")),
            "retryable": bool(details.get("retryable", False)),
            "details": details or None,
        }
    return {
        "id": str(record.id),
        "session_id": str(record.session_id),
        "snapshot_id": str(record.snapshot_id),
        "deployment_id": str(record.deployment_id),
        "retry_of_run_id": (
            str(record.retry_of_run_id) if record.retry_of_run_id else None
        ),
        "status": record.status,
        "result_quality": record.result_quality,
        "latest_sequence_no": record.latest_sequence_no,
        "error": error,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    run_id: UUID,
    action: str = "run.create",
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type="run",
            resource_id=run_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )


def _capacity_denial_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    resource_id: UUID | None,
    operation: Literal["create", "retry"],
    denial: CapacityAdmissionDenied,
    metadata: RequestMetadata,
) -> None:
    change = {
        "operation": operation,
        "scope": denial.scope,
        "reason_code": denial.reason_code,
        "current": denial.current,
        "limit": denial.limit,
    }
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=f"run.{operation}.admission_deny",
            resource_type="run",
            resource_id=resource_id,
            result="DENIED",
            reason_codes=["RATE_LIMITED", denial.reason_code],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )


def _reconciliation_audit(
    session: AsyncSession,
    *,
    context: TenantContext,
    run_id: UUID,
    action: str,
    change: dict[str, object],
    occurred_at: datetime,
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=UUID(context.tenant_id),
            actor_type=context.subject_type.value,
            actor_id=UUID(context.subject_id),
            action=action,
            resource_type="run",
            resource_id=run_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_json=change,
            created_at=occurred_at,
        )
    )
