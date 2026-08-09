"""Run route registration and frozen response-shape tests."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    RequestMetadata,
    RunEventPageRecord,
    RunEventQueryService,
    RunEventQueryStore,
    RunEventRecord,
    RunManagementService,
    RunStore,
)
from packages.contracts.public import AuthenticatedPrincipal, SubjectType, TenantContext
from packages.domain.public import MutationOutcome, RunRecord, TenantAccess
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
AGENT_ID = UUID("55555555-5555-4555-8555-555555555555")
SNAPSHOT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEPLOYMENT_ID = UUID("77777777-7777-4777-8777-777777777777")
MESSAGE_ID = UUID("88888888-8888-4888-8888-888888888888")
NOW = datetime(2026, 8, 8, tzinfo=UTC)


def record() -> RunRecord:
    return RunRecord(
        id=RUN_ID,
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        branch_id=None,
        user_message_id=MESSAGE_ID,
        assistant_message_id=None,
        agent_id=AGENT_ID,
        snapshot_id=SNAPSHOT_ID,
        deployment_id=DEPLOYMENT_ID,
        status="CREATED",
        result_quality=None,
        current_attempt=0,
        latest_sequence_no=0,
        idempotency_key="run-key-001",
        client_request_id=None,
        retry_of_run_id=None,
        timeout_seconds=600,
        token_budget=None,
        cost_budget_amount=cast(Decimal | None, None),
        cost_budget_currency=None,
        workflow_id=None,
        error_code=None,
        error_detail=None,
        created_by=ACTOR_ID,
        created_at=NOW,
        queued_at=None,
        started_at=None,
        finished_at=None,
    )


class Stub:
    def __init__(self) -> None:
        self.current_record = record()

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR_ID),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset(
                {
                    "session:read",
                    "run:cancel",
                    "run:create",
                    "run:read",
                    "run:list",
                    "run:retry",
                }
            ),
        )

    async def list_session_runs(self, _context: TenantContext, **_kwargs: object):
        return [record()], None

    async def create_run(self, _context: TenantContext, **_kwargs: object):
        return MutationOutcome(value=record())

    async def get_run(self, _context: TenantContext, **_kwargs: object):
        return self.current_record

    async def list_events(self, _context: TenantContext, **_kwargs: object):
        return RunEventPageRecord(
            events=(
                RunEventRecord(
                    id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    tenant_id=TENANT_ID,
                    run_id=RUN_ID,
                    session_id=SESSION_ID,
                    sequence_no=1,
                    source_event_id="source-run-created",
                    execution_attempt=1,
                    schema_version="1.0",
                    event_type="run_created",
                    payload_version="1.0",
                    payload={
                        "deployment_id": str(DEPLOYMENT_ID),
                        "snapshot_id": str(SNAPSHOT_ID),
                    },
                    occurred_at=NOW,
                    recorded_at=NOW,
                    trace_id="trace-run-event",
                ),
            ),
            latest_sequence_no=1,
            has_more=False,
        )

    async def request_cancel(self, _context: TenantContext, **_kwargs: object):
        self.current_record = replace(self.current_record, status="CANCELLING")
        return MutationOutcome(value=self.current_record)

    async def retry_run(self, _context: TenantContext, **_kwargs: object):
        retried = replace(
            record(),
            id=UUID("99999999-9999-4999-8999-999999999999"),
            retry_of_run_id=RUN_ID,
        )
        return MutationOutcome(value=retried)


def build_app():
    settings = AppSettings()
    stub = Stub()
    return create_app(
        settings,
        identity_provider=MockIdentityProvider(settings, now=lambda: NOW),
        run_service=RunManagementService(stub, cast(RunStore, stub)),
        run_event_query_service=RunEventQueryService(
            stub, cast(RunEventQueryStore, stub)
        ),
    )


def test_app_openapi_registers_frozen_run_operations() -> None:
    operation_ids = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )
    assert {
        "listSessionRuns",
        "createRun",
        "getRun",
        "listRunEvents",
        "cancelRun",
        "retryRun",
    } <= operation_ids


@pytest.mark.asyncio
async def test_list_run_events_returns_frozen_sequence_page() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{RUN_ID}/events?after=0&limit=20",
            headers={
                "Authorization": "Bearer mock",
                "X-Request-ID": "req-run-events",
            },
        )

    assert response.status_code == 200
    assert response.json()["latest_sequence_no"] == 1
    assert response.json()["has_more"] is False
    assert response.json()["items"][0]["sequence_no"] == 1
    assert response.json()["items"][0]["event_type"] == "run_created"


@pytest.mark.asyncio
async def test_create_run_returns_accepted_contract() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/runs",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "run-key-001",
                "X-Request-ID": "req-run-create",
            },
            json={"session_id": str(SESSION_ID), "input": {"text": "hello"}},
        )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "status": "CREATED",
        "events_url": f"/api/v1/runs/{RUN_ID}/events",
        "stream_url": f"/api/v1/runs/{RUN_ID}/events/stream",
    }


@pytest.mark.asyncio
async def test_get_run_returns_pinned_execution_identity() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{RUN_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-run-get"},
        )

    assert response.status_code == 200
    assert response.json()["snapshot_id"] == str(SNAPSHOT_ID)
    assert response.json()["deployment_id"] == str(DEPLOYMENT_ID)


@pytest.mark.asyncio
async def test_cancel_run_returns_cancelling_until_workflow_confirms_stop() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/cancel",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "cancel-key-001",
                "X-Request-ID": "req-run-cancel",
            },
            json={"reason": "stop"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "CANCELLING"


@pytest.mark.asyncio
async def test_retry_run_returns_new_accepted_run() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/retry",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "retry-key-001",
                "X-Request-ID": "req-run-retry",
            },
            json={"deployment_policy": "original_snapshot"},
        )

    assert response.status_code == 202
    assert response.json()["run_id"] == "99999999-9999-4999-8999-999999999999"


@pytest.mark.asyncio
async def test_unconfigured_run_service_returns_stable_dependency_error() -> None:
    settings = AppSettings()
    transport = httpx.ASGITransport(
        app=create_app(
            settings,
            identity_provider=MockIdentityProvider(settings, now=lambda: NOW),
        )
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{RUN_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-run-missing"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
