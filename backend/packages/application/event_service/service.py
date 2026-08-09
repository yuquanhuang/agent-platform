"""Authorize and validate RuntimeEventCandidate batches before persistence."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue

from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import (
    Error,
    RunEventAppendResult,
    RunEventBatchRequest,
    RunEventBatchResponse,
    RunEventPage,
)
from packages.contracts.generated.run_event import (
    RUN_EVENT_ADAPTER,
    RunEvent,
    RuntimeEventCandidate,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
    permission_denied,
    resource_not_found,
    run_event_sequence_gap,
    unauthenticated,
    validation_error,
)
from packages.domain.public import TenantAccess

EVENT_WRITE_PERMISSION = "internal:event_write"
MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
REDACTED_THINKING_DELTA = "Sensitive thinking content was redacted."


@dataclass(frozen=True, slots=True)
class EventWriteAccess:
    """Trusted workload identity resolved by the deployment authentication adapter."""

    context: TenantContext
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class EventAppendItem:
    source_event_id: str
    status: Literal["created", "duplicate", "rejected"]
    event_id: UUID | None = None
    sequence_no: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class EventBatchFailure:
    status_code: int
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class EventBatchStoreOutcome:
    items: tuple[EventAppendItem, ...] = ()
    failure: EventBatchFailure | None = None


class RunEventAppendStore(Protocol):
    async def append_batch(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        execution_fencing_token: str,
        events: tuple[RuntimeEventCandidate, ...],
    ) -> EventBatchStoreOutcome: ...


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    session_id: UUID
    sequence_no: int
    source_event_id: str
    execution_attempt: int
    schema_version: str
    event_type: str
    payload_version: str
    payload: dict[str, JsonValue]
    occurred_at: datetime
    recorded_at: datetime
    trace_id: str


@dataclass(frozen=True, slots=True)
class RunEventPageRecord:
    events: tuple[RunEventRecord, ...]
    latest_sequence_no: int
    has_more: bool


class RunEventQueryAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class RunEventQueryStore(Protocol):
    async def list_events(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        after: int,
        limit: int,
    ) -> RunEventPageRecord | None: ...


class RunEventIngestionService:
    """Validate one bounded batch and preserve per-candidate result ordering."""

    def __init__(self, store: RunEventAppendStore) -> None:
        self._store = store

    async def append_batch(
        self,
        access: EventWriteAccess,
        *,
        run_id: str,
        request: RunEventBatchRequest,
    ) -> RunEventBatchResponse:
        self._authorize(access)
        parsed_run_id = _run_id(run_id)
        valid_events: list[RuntimeEventCandidate] = []
        result_slots: list[RunEventAppendResult | None] = []
        for candidate in request.events:
            rejection = _validate_candidate(candidate, access.context.request_id)
            result_slots.append(rejection)
            if rejection is None:
                valid_events.append(candidate)

        persisted: tuple[EventAppendItem, ...] = ()
        if valid_events:
            outcome = await self._store.append_batch(
                access.context,
                run_id=parsed_run_id,
                execution_attempt=request.execution_attempt,
                execution_fencing_token=request.execution_fencing_token,
                events=tuple(valid_events),
            )
            if outcome.failure is not None:
                raise PlatformError(
                    status_code=outcome.failure.status_code,
                    code=outcome.failure.code,
                    message=outcome.failure.message,
                    retryable=outcome.failure.retryable,
                )
            persisted = outcome.items

        persisted_index = 0
        for index, current in enumerate(result_slots):
            if current is not None:
                continue
            if persisted_index >= len(persisted):
                raise RuntimeError("Event Store returned fewer results than candidates")
            result_slots[index] = _response_item(
                persisted[persisted_index], access.context.request_id
            )
            persisted_index += 1
        if persisted_index != len(persisted):
            raise RuntimeError("Event Store returned more results than candidates")
        return RunEventBatchResponse(
            items=[item for item in result_slots if item is not None]
        )

    @staticmethod
    def _authorize(access: EventWriteAccess) -> None:
        if access.context.subject_type is not SubjectType.SERVICE:
            raise unauthenticated("A service workload identity is required.")
        if EVENT_WRITE_PERMISSION not in access.permissions:
            raise permission_denied("The service identity cannot append Run events.")


class RunEventQueryService:
    """Authorize owned Run history reads and preserve contiguous replay semantics."""

    def __init__(
        self,
        access_resolver: RunEventQueryAccessResolver,
        store: RunEventQueryStore,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_events(
        self,
        principal: AuthenticatedPrincipal,
        *,
        run_id: str,
        after: int,
        limit: int,
        metadata: RequestMetadata,
    ) -> RunEventPage:
        if after < 0:
            raise validation_error("after must be greater than or equal to 0.")
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200.")
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("run", "read"):
            raise permission_denied()
        page = await self._store.list_events(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            run_id=_resource_id(run_id),
            after=after,
            limit=limit,
        )
        if page is None:
            raise resource_not_found()
        _validate_sequence_page(page, after=after)
        can_view_sensitive = access.allows("run", "view_sensitive")
        return RunEventPage(
            items=[
                _run_event(record, can_view_sensitive=can_view_sensitive)
                for record in page.events
            ],
            has_more=page.has_more,
            latest_sequence_no=page.latest_sequence_no,
        )


def _validate_candidate(
    candidate: RuntimeEventCandidate, request_id: str
) -> RunEventAppendResult | None:
    if candidate.occurred_at.utcoffset() is None:
        return _rejected(
            candidate.source_event_id,
            request_id,
            code="CONTRACT_VALIDATION_FAILED",
            message="occurred_at must be timezone-aware.",
        )
    payload = candidate.payload.model_dump(mode="json")
    payload_size = len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if payload_size > MAX_EVENT_PAYLOAD_BYTES:
        return _rejected(
            candidate.source_event_id,
            request_id,
            code="EVENT_PAYLOAD_TOO_LARGE",
            message="The event payload exceeds 256KB and must use an Artifact.",
        )
    return None


def _response_item(item: EventAppendItem, request_id: str) -> RunEventAppendResult:
    error = None
    if item.status == "rejected":
        if item.error_code is None or item.error_message is None:
            raise RuntimeError("Rejected Event Store result is missing an error")
        error = Error(
            code=item.error_code,
            message=item.error_message,
            request_id=request_id,
            retryable=False,
        )
    return RunEventAppendResult(
        source_event_id=item.source_event_id,
        status=item.status,
        event_id=str(item.event_id) if item.event_id is not None else None,
        sequence_no=item.sequence_no,
        error=error,
    )


def _rejected(
    source_event_id: str,
    request_id: str,
    *,
    code: str,
    message: str,
) -> RunEventAppendResult:
    return RunEventAppendResult(
        source_event_id=source_event_id,
        status="rejected",
        event_id=None,
        sequence_no=None,
        error=Error(
            code=code,
            message=message,
            request_id=request_id,
            retryable=False,
        ),
    )


def _run_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise validation_error("run_id must be a UUID.") from error


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise validation_error("Resource identifier is invalid.") from error


def _validate_sequence_page(page: RunEventPageRecord, *, after: int) -> None:
    expected = after + 1
    for event in page.events:
        if event.sequence_no != expected:
            raise run_event_sequence_gap(
                after=after,
                expected_sequence_no=expected,
                observed_sequence_no=event.sequence_no,
                latest_sequence_no=page.latest_sequence_no,
            )
        expected += 1
    if page.has_more and not page.events:
        raise RuntimeError("RunEvent Store returned has_more without page events")
    if after < page.latest_sequence_no and not page.events:
        raise run_event_sequence_gap(
            after=after,
            expected_sequence_no=after + 1,
            observed_sequence_no=None,
            latest_sequence_no=page.latest_sequence_no,
        )
    if (
        page.events
        and not page.has_more
        and page.events[-1].sequence_no < page.latest_sequence_no
    ):
        raise run_event_sequence_gap(
            after=after,
            expected_sequence_no=page.events[-1].sequence_no + 1,
            observed_sequence_no=None,
            latest_sequence_no=page.latest_sequence_no,
        )


def _run_event(record: RunEventRecord, *, can_view_sensitive: bool) -> RunEvent:
    payload = dict(record.payload)
    if record.event_type == "thinking_delta" and not can_view_sensitive:
        payload["delta"] = REDACTED_THINKING_DELTA
    return RUN_EVENT_ADAPTER.validate_python(
        {
            "schema_version": record.schema_version,
            "event_id": str(record.id),
            "source_event_id": record.source_event_id,
            "tenant_id": str(record.tenant_id),
            "run_id": str(record.run_id),
            "session_id": str(record.session_id),
            "sequence_no": record.sequence_no,
            "event_type": record.event_type,
            "occurred_at": record.occurred_at,
            "recorded_at": record.recorded_at,
            "trace_id": record.trace_id,
            "execution_attempt": record.execution_attempt,
            "payload_version": record.payload_version,
            "payload": payload,
        }
    )
