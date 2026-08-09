"""PostgreSQL RunEvent ingestion with fenced, contiguous sequence allocation."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.event_service import (
    EventAppendItem,
    EventBatchFailure,
    EventBatchStoreOutcome,
    EventWriteAccess,
    RunEventIngestionService,
    RunEventPageRecord,
    RunEventRecord,
)
from packages.application.temporal import RunExecutionRequest
from packages.contracts.generated.core_models import RunEventBatchRequest
from packages.contracts.generated.run_event import RuntimeEventCandidate
from packages.contracts.public import (
    PlatformError,
    TenantContext,
    dependency_unavailable,
    run_event_sequence_gap,
    validation_error,
)
from packages.domain.public import OutboxEvent, OutboxStatus
from packages.infrastructure.database.models import (
    AgentRunModel,
    AuditLogModel,
    ChatSessionModel,
    RunAttemptModel,
    RunEventCounterModel,
    RunEventModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxWriter
from packages.infrastructure.database.uow import TenantUnitOfWork

RUN_EVENTS_APPENDED_EVENT = "run.events_appended.v1"
TERMINAL_EVENT_STATUSES = {
    "run_succeeded": "SUCCEEDED",
    "run_failed": "FAILED",
    "run_cancelled": "CANCELLED",
    "run_timeout": "TIMEOUT",
}


@dataclass(frozen=True, slots=True)
class _MaterializedEvent:
    event_id: UUID
    sequence_no: int
    source_event_id: str
    event_type: str
    payload_version: str
    payload: dict[str, object]
    occurred_at: datetime


class SqlAlchemyRunEventStore:
    """Serialize appends per Run and commit events, cursor and Outbox atomically."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append_batch(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        execution_fencing_token: str,
        events: tuple[RuntimeEventCandidate, ...],
    ) -> EventBatchStoreOutcome:
        try:
            async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
                return await self._append_in_transaction(
                    unit_of_work.session,
                    context,
                    run_id=run_id,
                    execution_attempt=execution_attempt,
                    execution_fencing_token=execution_fencing_token,
                    events=events,
                )
        except SQLAlchemyError as error:
            raise dependency_unavailable("Run Event Store is unavailable.") from error

    async def _append_in_transaction(
        self,
        session: AsyncSession,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        execution_fencing_token: str,
        events: tuple[RuntimeEventCandidate, ...],
    ) -> EventBatchStoreOutcome:
        tenant_id = UUID(context.tenant_id)
        run = await session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.tenant_id == tenant_id,
                AgentRunModel.id == run_id,
            )
            .with_for_update()
        )
        if run is None:
            return EventBatchStoreOutcome(
                failure=EventBatchFailure(
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message="The Run is unavailable.",
                )
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
        supplied_hash = _sha256(execution_fencing_token)
        if (
            attempt is None
            or run.current_attempt != execution_attempt
            or not hmac.compare_digest(attempt.fencing_token_hash, supplied_hash)
        ):
            _add_audit(
                session,
                context,
                action="execution_fencing_rejected",
                run_id=run_id,
                result="DENIED",
                reason_codes=["EXECUTION_FENCING_REJECTED"],
                metadata={"execution_attempt": execution_attempt},
            )
            return EventBatchStoreOutcome(
                failure=EventBatchFailure(
                    status_code=409,
                    code="EXECUTION_FENCING_REJECTED",
                    message="The execution fencing token is stale or invalid.",
                )
            )

        source_ids = {candidate.source_event_id for candidate in events}
        existing_rows = list(
            (
                await session.scalars(
                    select(RunEventModel).where(
                        RunEventModel.tenant_id == tenant_id,
                        RunEventModel.run_id == run_id,
                        RunEventModel.execution_attempt == execution_attempt,
                        RunEventModel.source_event_id.in_(source_ids),
                    )
                )
            ).all()
        )
        by_source = {row.source_event_id: _from_row(row) for row in existing_rows}
        terminal_row = await session.scalar(
            select(RunEventModel)
            .where(
                RunEventModel.tenant_id == tenant_id,
                RunEventModel.run_id == run_id,
                RunEventModel.event_type.in_(tuple(TERMINAL_EVENT_STATUSES)),
            )
            .order_by(RunEventModel.sequence_no)
            .limit(1)
        )
        terminal = _from_row(terminal_row) if terminal_row is not None else None

        results: list[EventAppendItem | None] = []
        pending: list[tuple[int, RuntimeEventCandidate]] = []
        pending_by_source: dict[str, tuple[int, RuntimeEventCandidate]] = {}
        pending_terminal: tuple[int, RuntimeEventCandidate] | None = None
        pending_duplicates: list[tuple[int, int]] = []
        for candidate in events:
            known = by_source.get(candidate.source_event_id)
            if known is not None:
                if _same_idempotent_candidate(known, candidate):
                    results.append(_duplicate(candidate.source_event_id, known))
                else:
                    results.append(
                        _rejected(
                            candidate.source_event_id,
                            "IDEMPOTENCY_KEY_REUSED",
                            "source_event_id was reused for different event content.",
                        )
                    )
                continue
            same_batch = pending_by_source.get(candidate.source_event_id)
            if same_batch is not None:
                primary_index, primary = same_batch
                if _same_candidate(primary, candidate):
                    results.append(None)
                    pending_duplicates.append((len(results) - 1, primary_index))
                else:
                    results.append(
                        _rejected(
                            candidate.source_event_id,
                            "IDEMPOTENCY_KEY_REUSED",
                            "source_event_id was reused for different event content.",
                        )
                    )
                continue
            if candidate.event_type in TERMINAL_EVENT_STATUSES:
                if terminal is not None:
                    if _same_terminal_event(terminal, candidate):
                        results.append(_duplicate(candidate.source_event_id, terminal))
                    else:
                        results.append(
                            _terminal_conflict(
                                session, context, run_id, candidate.source_event_id
                            )
                        )
                    continue
                if pending_terminal is not None:
                    primary_index, primary = pending_terminal
                    if _same_candidate(primary, candidate, include_occurred_at=False):
                        results.append(None)
                        pending_duplicates.append((len(results) - 1, primary_index))
                    else:
                        results.append(
                            _terminal_conflict(
                                session, context, run_id, candidate.source_event_id
                            )
                        )
                    continue
                terminal_rejection = _validate_terminal_materialization(run, candidate)
                if terminal_rejection is not None:
                    results.append(terminal_rejection)
                    _add_audit(
                        session,
                        context,
                        action="terminal_conflict",
                        run_id=run_id,
                        result="DENIED",
                        reason_codes=[
                            terminal_rejection.error_code or "TERMINAL_CONFLICT"
                        ],
                        metadata={
                            "execution_attempt": execution_attempt,
                            "event_type": candidate.event_type,
                        },
                    )
                    continue
            results.append(None)
            result_index = len(results) - 1
            pending.append((result_index, candidate))
            pending_by_source[candidate.source_event_id] = (result_index, candidate)
            if candidate.event_type in TERMINAL_EVENT_STATUSES:
                pending_terminal = (result_index, candidate)

        if not pending:
            return EventBatchStoreOutcome(items=tuple(_complete_results(results)))

        await session.execute(
            insert(RunEventCounterModel)
            .values(run_id=run_id, tenant_id=tenant_id, next_sequence_no=1)
            .on_conflict_do_nothing(index_elements=[RunEventCounterModel.run_id])
        )
        counter = await session.scalar(
            select(RunEventCounterModel)
            .where(
                RunEventCounterModel.tenant_id == tenant_id,
                RunEventCounterModel.run_id == run_id,
            )
            .with_for_update()
        )
        if counter is None:
            raise RuntimeError("RunEvent counter could not be initialized")
        expected_next = run.latest_sequence_no + 1
        if counter.next_sequence_no != expected_next:
            raise RuntimeError("RunEvent counter and Run cursor are inconsistent")

        recorded_at = datetime.now(UTC)
        first_sequence_no = counter.next_sequence_no
        created: list[_MaterializedEvent] = []
        for offset, (result_index, candidate) in enumerate(pending):
            sequence_no = first_sequence_no + offset
            event_id = _event_id(
                tenant_id,
                run_id,
                execution_attempt,
                candidate.source_event_id,
            )
            payload = candidate.payload.model_dump(mode="json")
            session.add(
                RunEventModel(
                    id=event_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    session_id=run.session_id,
                    sequence_no=sequence_no,
                    source_event_id=candidate.source_event_id,
                    execution_attempt=execution_attempt,
                    schema_version="1.0",
                    event_type=candidate.event_type,
                    payload_version=candidate.payload_version,
                    payload_json=payload,
                    occurred_at=candidate.occurred_at,
                    recorded_at=recorded_at,
                    trace_id=context.trace_id,
                )
            )
            materialized = _MaterializedEvent(
                event_id=event_id,
                sequence_no=sequence_no,
                source_event_id=candidate.source_event_id,
                event_type=candidate.event_type,
                payload_version=candidate.payload_version,
                payload=payload,
                occurred_at=candidate.occurred_at,
            )
            created.append(materialized)
            by_source[candidate.source_event_id] = materialized
            if candidate.event_type in TERMINAL_EVENT_STATUSES:
                terminal = materialized
            results[result_index] = EventAppendItem(
                source_event_id=candidate.source_event_id,
                status="created",
                event_id=event_id,
                sequence_no=sequence_no,
            )

        for duplicate_index, primary_index in pending_duplicates:
            primary_result = results[primary_index]
            if primary_result is None or primary_result.event_id is None:
                raise RuntimeError("RunEvent duplicate source is missing its primary")
            results[duplicate_index] = EventAppendItem(
                source_event_id=events[duplicate_index].source_event_id,
                status="duplicate",
                event_id=primary_result.event_id,
                sequence_no=primary_result.sequence_no,
            )

        next_sequence_no = first_sequence_no + len(created)
        counter.next_sequence_no = next_sequence_no
        run.latest_sequence_no = next_sequence_no - 1
        SqlAlchemyOutboxWriter(session, context).add(
            OutboxEvent(
                id=uuid5(
                    NAMESPACE_URL,
                    f"run-events-outbox/{tenant_id}/{run_id}/"
                    f"{first_sequence_no}/{next_sequence_no - 1}",
                ),
                tenant_id=tenant_id,
                aggregate_type="run_event",
                aggregate_id=run_id,
                event_type=RUN_EVENTS_APPENDED_EVENT,
                payload={
                    "tenant_id": str(tenant_id),
                    "run_id": str(run_id),
                    "first_sequence_no": first_sequence_no,
                    "last_sequence_no": next_sequence_no - 1,
                    "event_count": len(created),
                },
                payload_schema_version=1,
                status=OutboxStatus.PENDING,
                attempts=0,
                next_attempt_at=recorded_at,
                created_at=recorded_at,
            )
        )
        return EventBatchStoreOutcome(items=tuple(_complete_results(results)))


class SqlAlchemyRunEventQueryStore:
    """Read one ownership-scoped, cursor-consistent RunEvent history page."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_events(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        after: int,
        limit: int,
    ) -> RunEventPageRecord | None:
        if after < 0:
            raise validation_error("after must be greater than or equal to 0.")
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200.")
        tenant_id = UUID(context.tenant_id)
        try:
            async with TenantUnitOfWork(
                self._session_factory, context, read_only=True
            ) as unit_of_work:
                run = await unit_of_work.session.scalar(
                    select(AgentRunModel)
                    .join(
                        ChatSessionModel,
                        (ChatSessionModel.tenant_id == AgentRunModel.tenant_id)
                        & (ChatSessionModel.id == AgentRunModel.session_id),
                    )
                    .where(
                        AgentRunModel.tenant_id == tenant_id,
                        AgentRunModel.id == run_id,
                        ChatSessionModel.user_id == user_id,
                        ChatSessionModel.status != "DELETED",
                    )
                )
                if run is None:
                    return None
                latest_sequence_no = run.latest_sequence_no
                if after >= latest_sequence_no:
                    return RunEventPageRecord(
                        events=(),
                        latest_sequence_no=latest_sequence_no,
                        has_more=False,
                    )
                rows = list(
                    (
                        await unit_of_work.session.scalars(
                            select(RunEventModel)
                            .where(
                                RunEventModel.tenant_id == tenant_id,
                                RunEventModel.run_id == run_id,
                                RunEventModel.sequence_no > after,
                                RunEventModel.sequence_no <= latest_sequence_no,
                            )
                            .order_by(RunEventModel.sequence_no)
                            .limit(limit + 1)
                        )
                    ).all()
                )
                _ensure_contiguous_rows(
                    rows,
                    after=after,
                    limit=limit,
                    latest_sequence_no=latest_sequence_no,
                )
                return RunEventPageRecord(
                    events=tuple(_event_record(row) for row in rows[:limit]),
                    latest_sequence_no=latest_sequence_no,
                    has_more=len(rows) > limit,
                )
        except PlatformError:
            raise
        except SQLAlchemyError as error:
            raise dependency_unavailable("Run Event Store is unavailable.") from error


class SqlAlchemyRuntimeEventCandidatePublisher:
    """Bridge one Runtime Activity candidate into the durable Event Service."""

    def __init__(self, service: RunEventIngestionService) -> None:
        self._service = service

    async def publish(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        candidate: RuntimeEventCandidate,
    ) -> None:
        response = await self._service.append_batch(
            EventWriteAccess(
                context=context,
                permissions=frozenset({"internal:event_write"}),
            ),
            run_id=str(request.run_id),
            request=RunEventBatchRequest(
                execution_attempt=request.execution_attempt,
                execution_fencing_token=request.fencing_token.get_secret_value(),
                events=[candidate],
            ),
        )
        result = response.items[0]
        if result.status == "rejected":
            if result.error is None:
                raise RuntimeError("Rejected Runtime event is missing an error")
            raise PlatformError(
                status_code=409,
                code=result.error.code,
                message=result.error.message,
                retryable=result.error.retryable,
            )


def _ensure_contiguous_rows(
    rows: list[RunEventModel],
    *,
    after: int,
    limit: int,
    latest_sequence_no: int,
) -> None:
    expected = after + 1
    for row in rows:
        if row.sequence_no != expected:
            raise run_event_sequence_gap(
                after=after,
                expected_sequence_no=expected,
                observed_sequence_no=row.sequence_no,
                latest_sequence_no=latest_sequence_no,
            )
        expected += 1
    if not rows and after < latest_sequence_no:
        raise run_event_sequence_gap(
            after=after,
            expected_sequence_no=after + 1,
            observed_sequence_no=None,
            latest_sequence_no=latest_sequence_no,
        )
    if len(rows) <= limit and rows and rows[-1].sequence_no < latest_sequence_no:
        raise run_event_sequence_gap(
            after=after,
            expected_sequence_no=rows[-1].sequence_no + 1,
            observed_sequence_no=None,
            latest_sequence_no=latest_sequence_no,
        )


def _event_record(row: RunEventModel) -> RunEventRecord:
    return RunEventRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        session_id=row.session_id,
        sequence_no=row.sequence_no,
        source_event_id=row.source_event_id,
        execution_attempt=row.execution_attempt,
        schema_version=row.schema_version,
        event_type=row.event_type,
        payload_version=row.payload_version,
        payload=cast(dict[str, JsonValue], row.payload_json),
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        trace_id=row.trace_id,
    )


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _event_id(
    tenant_id: UUID, run_id: UUID, execution_attempt: int, source_event_id: str
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"run-event/{tenant_id}/{run_id}/{execution_attempt}/{source_event_id}",
    )


def _from_row(row: RunEventModel) -> _MaterializedEvent:
    return _MaterializedEvent(
        event_id=row.id,
        sequence_no=row.sequence_no,
        source_event_id=row.source_event_id,
        event_type=row.event_type,
        payload_version=row.payload_version,
        payload=row.payload_json,
        occurred_at=row.occurred_at,
    )


def _same_idempotent_candidate(
    existing: _MaterializedEvent, candidate: RuntimeEventCandidate
) -> bool:
    return (
        existing.event_type == candidate.event_type
        and existing.payload_version == candidate.payload_version
        and existing.payload == candidate.payload.model_dump(mode="json")
        and existing.occurred_at == candidate.occurred_at
    )


def _same_terminal_event(
    existing: _MaterializedEvent, candidate: RuntimeEventCandidate
) -> bool:
    return (
        existing.event_type == candidate.event_type
        and existing.payload_version == candidate.payload_version
        and existing.payload == candidate.payload.model_dump(mode="json")
    )


def _same_candidate(
    first: RuntimeEventCandidate,
    second: RuntimeEventCandidate,
    *,
    include_occurred_at: bool = True,
) -> bool:
    return (
        first.event_type == second.event_type
        and first.payload_version == second.payload_version
        and first.payload.model_dump(mode="json")
        == second.payload.model_dump(mode="json")
        and (not include_occurred_at or first.occurred_at == second.occurred_at)
    )


def _duplicate(source_event_id: str, existing: _MaterializedEvent) -> EventAppendItem:
    return EventAppendItem(
        source_event_id=source_event_id,
        status="duplicate",
        event_id=existing.event_id,
        sequence_no=existing.sequence_no,
    )


def _rejected(source_event_id: str, code: str, message: str) -> EventAppendItem:
    return EventAppendItem(
        source_event_id=source_event_id,
        status="rejected",
        error_code=code,
        error_message=message,
    )


def _terminal_conflict(
    session: AsyncSession,
    context: TenantContext,
    run_id: UUID,
    source_event_id: str,
) -> EventAppendItem:
    _add_audit(
        session,
        context,
        action="terminal_conflict",
        run_id=run_id,
        result="DENIED",
        reason_codes=["RUN_TERMINAL_EVENT_CONFLICT"],
        metadata={"source_event_id": source_event_id},
    )
    return _rejected(
        source_event_id,
        "RUN_TERMINAL_EVENT_CONFLICT",
        "The Run already has a different terminal event.",
    )


def _validate_terminal_materialization(
    run: AgentRunModel, candidate: RuntimeEventCandidate
) -> EventAppendItem | None:
    expected_status = TERMINAL_EVENT_STATUSES[candidate.event_type]
    if run.status != expected_status:
        return _rejected(
            candidate.source_event_id,
            "RUN_TERMINAL_NOT_FINALIZED",
            "The Run terminal state must be finalized before its terminal event.",
        )
    if candidate.event_type == "run_succeeded":
        try:
            result_message_id = UUID(candidate.payload.result_message_id)
        except ValueError:
            return _rejected(
                candidate.source_event_id,
                "RUN_TERMINAL_RESULT_MISMATCH",
                "The terminal event result Message identifier is invalid.",
            )
        if run.assistant_message_id != result_message_id:
            return _rejected(
                candidate.source_event_id,
                "RUN_TERMINAL_RESULT_MISMATCH",
                "The terminal event does not reference the finalized result Message.",
            )
    if (
        candidate.event_type == "run_failed"
        and run.error_code != candidate.payload.error_code
    ):
        return _rejected(
            candidate.source_event_id,
            "RUN_TERMINAL_RESULT_MISMATCH",
            "The terminal event error does not match the finalized Run.",
        )
    return None


def _complete_results(
    results: list[EventAppendItem | None],
) -> list[EventAppendItem]:
    if any(item is None for item in results):
        raise RuntimeError("RunEvent append result is incomplete")
    return [item for item in results if item is not None]


def _add_audit(
    session: AsyncSession,
    context: TenantContext,
    *,
    action: str,
    run_id: UUID,
    result: str,
    reason_codes: list[str],
    metadata: dict[str, object],
) -> None:
    session.add(
        AuditLogModel(
            id=uuid4(),
            tenant_id=UUID(context.tenant_id),
            actor_type="service",
            actor_id=UUID(context.subject_id),
            action=action,
            resource_type="run",
            resource_id=run_id,
            result=result,
            reason_codes=reason_codes,
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_schema_version=1,
            metadata_json=metadata,
        )
    )
