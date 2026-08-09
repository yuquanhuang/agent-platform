"""RunEvent ingestion authorization, validation and result mapping tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.application.event_service import (
    EventAppendItem,
    EventBatchFailure,
    EventBatchStoreOutcome,
    EventWriteAccess,
    RunEventIngestionService,
)
from packages.contracts.generated.core_models import RunEventBatchRequest
from packages.contracts.generated.run_event import (
    RUNTIME_EVENT_CANDIDATE_ADAPTER,
    RuntimeEventCandidate,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext

NOW = datetime(2026, 8, 8, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
SERVICE_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")


def access(
    *,
    subject_type: SubjectType = SubjectType.SERVICE,
    permissions: frozenset[str] = frozenset({"internal:event_write"}),
) -> EventWriteAccess:
    return EventWriteAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=subject_type,
            subject_id=str(SERVICE_ID),
            membership_version=1 if subject_type is SubjectType.USER else None,
            auth_time=NOW,
            request_id="req-event-service",
            trace_id="trace-event-service",
        ),
        permissions=permissions,
    )


def candidate(
    *, source_event_id: str = "source-1", occurred_at: datetime = NOW
) -> RuntimeEventCandidate:
    return RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
        {
            "source_event_id": source_event_id,
            "event_type": "text_delta",
            "occurred_at": occurred_at,
            "payload_version": "1.0",
            "payload": {"message_id": "message-1", "delta": "hello"},
        }
    )


def request(*events: RuntimeEventCandidate) -> RunEventBatchRequest:
    return RunEventBatchRequest(
        execution_attempt=1,
        execution_fencing_token="fencing-token-0001",
        events=list(events),
    )


class Store:
    def __init__(self, outcome: EventBatchStoreOutcome) -> None:
        self.outcome = outcome
        self.events = ()

    async def append_batch(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        execution_fencing_token: str,
        events: tuple[RuntimeEventCandidate, ...],
    ) -> EventBatchStoreOutcome:
        del context, run_id, execution_attempt, execution_fencing_token
        self.events = events
        return self.outcome


@pytest.mark.asyncio
async def test_service_maps_created_duplicate_and_rejected_items_in_request_order() -> (
    None
):
    store = Store(
        EventBatchStoreOutcome(
            items=(
                EventAppendItem(
                    source_event_id="source-1",
                    status="created",
                    event_id=EVENT_ID,
                    sequence_no=1,
                ),
                EventAppendItem(
                    source_event_id="source-2",
                    status="rejected",
                    error_code="IDEMPOTENCY_KEY_REUSED",
                    error_message="conflict",
                ),
            )
        )
    )
    service = RunEventIngestionService(store)

    response = await service.append_batch(
        access(),
        run_id=str(RUN_ID),
        request=request(candidate(), candidate(source_event_id="source-2")),
    )

    assert [item.status for item in response.items] == ["created", "rejected"]
    assert response.items[0].event_id == str(EVENT_ID)
    assert response.items[1].error is not None
    assert response.items[1].error.request_id == "req-event-service"


@pytest.mark.asyncio
async def test_service_rejects_naive_occurred_at_without_calling_store() -> None:
    store = Store(EventBatchStoreOutcome())
    service = RunEventIngestionService(store)

    response = await service.append_batch(
        access(),
        run_id=str(RUN_ID),
        request=request(candidate(occurred_at=NOW.replace(tzinfo=None))),
    )

    assert store.events == ()
    assert response.items[0].status == "rejected"
    assert response.items[0].error is not None
    assert response.items[0].error.code == "CONTRACT_VALIDATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_access", "code"),
    [
        (access(subject_type=SubjectType.USER), "UNAUTHENTICATED"),
        (access(permissions=frozenset()), "PERMISSION_DENIED"),
    ],
)
async def test_service_requires_service_identity_and_event_write_permission(
    event_access: EventWriteAccess, code: str
) -> None:
    service = RunEventIngestionService(Store(EventBatchStoreOutcome()))

    with pytest.raises(PlatformError) as denied:
        await service.append_batch(
            event_access,
            run_id=str(RUN_ID),
            request=request(candidate()),
        )

    assert denied.value.code == code


@pytest.mark.asyncio
async def test_service_raises_batch_failure_after_store_commits_audit() -> None:
    service = RunEventIngestionService(
        Store(
            EventBatchStoreOutcome(
                failure=EventBatchFailure(
                    status_code=409,
                    code="EXECUTION_FENCING_REJECTED",
                    message="stale",
                )
            )
        )
    )

    with pytest.raises(PlatformError) as rejected:
        await service.append_batch(
            access(),
            run_id=str(RUN_ID),
            request=request(candidate()),
        )

    assert rejected.value.status_code == 409
    assert rejected.value.code == "EXECUTION_FENCING_REJECTED"
