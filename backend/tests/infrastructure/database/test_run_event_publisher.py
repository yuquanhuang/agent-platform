"""RuntimeEventCandidate Publisher bridge tests without a database."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.application.event_service import (
    EventAppendItem,
    EventBatchStoreOutcome,
    RunEventIngestionService,
)
from packages.application.temporal import RunExecutionRequest
from packages.contracts.generated.run_event import (
    RUNTIME_EVENT_CANDIDATE_ADAPTER,
    RuntimeEventCandidate,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext
from packages.contracts.temporal import RunSpecReference
from packages.infrastructure.database.events import (
    SqlAlchemyRuntimeEventCandidatePublisher,
)

NOW = datetime(2026, 8, 8, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(RUN_ID),
        auth_time=NOW,
        request_id="req-runtime-publisher",
        trace_id="trace-runtime-publisher",
    )


def candidate() -> RuntimeEventCandidate:
    return RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
        {
            "source_event_id": "runtime-source-1",
            "event_type": "text_delta",
            "occurred_at": NOW,
            "payload_version": "1.0",
            "payload": {"message_id": "message-1", "delta": "hello"},
        }
    )


def execution_request() -> RunExecutionRequest:
    return RunExecutionRequest(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=2,
        run_spec=RunSpecReference(
            uri="memory://run-spec",
            content_hash="sha256:" + "a" * 64,
            size_bytes=10,
        ),
        timeout_seconds=60,
        runtime_type="agentscope",
        fencing_token=SecretStr("runtime-fencing-token-0002"),
    )


class Store:
    def __init__(self, result: EventAppendItem) -> None:
        self.result = result
        self.execution_attempt = 0
        self.fencing_token = ""

    async def append_batch(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        execution_fencing_token: str,
        events: tuple[RuntimeEventCandidate, ...],
    ) -> EventBatchStoreOutcome:
        assert context.subject_type is SubjectType.SERVICE
        assert run_id == RUN_ID
        assert events == (candidate(),)
        self.execution_attempt = execution_attempt
        self.fencing_token = execution_fencing_token
        return EventBatchStoreOutcome(items=(self.result,))


@pytest.mark.asyncio
async def test_runtime_publisher_forwards_attempt_and_plaintext_token_only_in_memory() -> (
    None
):
    store = Store(
        EventAppendItem(
            source_event_id="runtime-source-1",
            status="created",
            event_id=UUID("33333333-3333-4333-8333-333333333333"),
            sequence_no=1,
        )
    )
    publisher = SqlAlchemyRuntimeEventCandidatePublisher(
        RunEventIngestionService(store)
    )

    await publisher.publish(
        context(), request=execution_request(), candidate=candidate()
    )

    assert store.execution_attempt == 2
    assert store.fencing_token == "runtime-fencing-token-0002"


@pytest.mark.asyncio
async def test_runtime_publisher_fails_activity_when_candidate_is_rejected() -> None:
    publisher = SqlAlchemyRuntimeEventCandidatePublisher(
        RunEventIngestionService(
            Store(
                EventAppendItem(
                    source_event_id="runtime-source-1",
                    status="rejected",
                    error_code="IDEMPOTENCY_KEY_REUSED",
                    error_message="conflict",
                )
            )
        )
    )

    with pytest.raises(PlatformError) as rejected:
        await publisher.publish(
            context(), request=execution_request(), candidate=candidate()
        )

    assert rejected.value.code == "IDEMPOTENCY_KEY_REUSED"
