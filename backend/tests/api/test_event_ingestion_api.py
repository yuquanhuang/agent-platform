"""Internal RunEvent batch endpoint authentication and contract tests."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import Request

from apps.api.app import create_app
from packages.application.event_service import (
    EventAppendItem,
    EventBatchStoreOutcome,
    EventWriteAccess,
    RunEventIngestionService,
)
from packages.contracts.generated.run_event import RuntimeEventCandidate
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.public import AppSettings

NOW = datetime(2026, 8, 8, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
SERVICE_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")


class IdentityProvider:
    async def authenticate(self, request: Request) -> EventWriteAccess:
        return EventWriteAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.SERVICE,
                subject_id=str(SERVICE_ID),
                auth_time=NOW,
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
            ),
            permissions=frozenset({"internal:event_write"}),
        )


class Store:
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
        event = events[0]
        return EventBatchStoreOutcome(
            items=(
                EventAppendItem(
                    source_event_id=event.source_event_id,
                    status="created",
                    event_id=EVENT_ID,
                    sequence_no=1,
                ),
            )
        )


def body() -> dict[str, object]:
    return {
        "execution_attempt": 1,
        "execution_fencing_token": "fencing-token-0001",
        "events": [
            {
                "source_event_id": "source-1",
                "event_type": "text_delta",
                "occurred_at": NOW.isoformat(),
                "payload_version": "1.0",
                "payload": {"message_id": "message-1", "delta": "hello"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_internal_event_endpoint_returns_per_candidate_result() -> None:
    app = create_app(
        AppSettings(),
        internal_service_identity_provider=IdentityProvider(),
        event_ingestion_service=RunEventIngestionService(Store()),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/internal/v1/runs/{RUN_ID}/events:batch",
            headers={"X-Request-ID": "req-event-api"},
            json=body(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "source_event_id": "source-1",
                "status": "created",
                "event_id": str(EVENT_ID),
                "sequence_no": 1,
                "error": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_internal_event_endpoint_fails_closed_when_auth_adapter_is_missing() -> (
    None
):
    transport = httpx.ASGITransport(app=create_app(AppSettings()))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/internal/v1/runs/{RUN_ID}/events:batch",
            json=body(),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_openapi_registers_frozen_internal_event_operation() -> None:
    operation = create_app(AppSettings()).openapi()["paths"][
        "/internal/v1/runs/{run_id}/events:batch"
    ]["post"]

    assert operation["operationId"] == "appendRunEventCandidates"
