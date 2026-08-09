"""Run application authorization, ownership and frozen DTO mapping."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    RequestMetadata,
    RunManagementService,
    RunStore,
    RunWorkflowControl,
)
from packages.contracts.generated.core_models import (
    CancelRunRequest,
    RetryRunRequest,
    RunCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.contracts.temporal import CancelRunSignal
from packages.domain.public import MutationOutcome, RunRecord, TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
AGENT_ID = UUID("55555555-5555-4555-8555-555555555555")
SNAPSHOT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEPLOYMENT_ID = UUID("77777777-7777-4777-8777-777777777777")
MESSAGE_ID = UUID("88888888-8888-4888-8888-888888888888")
NOW = datetime(2026, 8, 8, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-run", trace_id="trace-run")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="run-user",
        display_name="Run User",
        platform_roles=frozenset(),
        auth_time=NOW,
    )


def access(*permissions: str) -> TenantAccess:
    return TenantAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.USER,
            subject_id=str(ACTOR_ID),
            membership_version=1,
            auth_time=NOW,
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset(permissions),
    )


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
        token_budget=1000,
        cost_budget_amount=Decimal("1.25"),
        cost_budget_currency="USD",
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
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access
        self.user_id: UUID | None = None
        self.request_hash: str | None = None
        self.current_record = record()

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal, metadata
        return self.tenant_access

    async def list_session_runs(self, _context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        return [record()], None

    async def create_run(self, _context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        self.request_hash = cast(str, kwargs["request_hash"])
        return MutationOutcome(value=record())

    async def get_run(self, _context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        return self.current_record

    async def request_cancel(self, _context: TenantContext, **kwargs: object):
        self.request_hash = cast(str, kwargs["request_hash"])
        self.current_record = replace(self.current_record, status="CANCELLING")
        return MutationOutcome(value=self.current_record)

    async def retry_run(self, _context: TenantContext, **kwargs: object):
        self.request_hash = cast(str, kwargs["request_hash"])
        retried = replace(
            record(),
            id=UUID("99999999-9999-4999-8999-999999999999"),
            retry_of_run_id=RUN_ID,
        )
        return MutationOutcome(value=retried)


class WorkflowControlStub:
    def __init__(self) -> None:
        self.signals: list[CancelRunSignal] = []

    async def signal_cancel(self, **kwargs: object) -> None:
        self.signals.append(cast(CancelRunSignal, kwargs["signal"]))


def service(
    stub: Stub, workflow_control: WorkflowControlStub | None = None
) -> RunManagementService:
    return RunManagementService(
        stub,
        cast(RunStore, stub),
        cast(RunWorkflowControl, workflow_control) if workflow_control else None,
    )


@pytest.mark.asyncio
async def test_create_run_maps_accepted_urls_and_scopes_to_actor() -> None:
    stub = Stub(access("run:create"))
    result = await service(stub).create_run(
        principal(),
        request=RunCreateRequest.model_validate(
            {"session_id": str(SESSION_ID), "input": {"text": "hello"}}
        ),
        idempotency_key="run-key-001",
        metadata=METADATA,
    )

    assert stub.user_id == ACTOR_ID
    assert stub.request_hash is not None and len(stub.request_hash) == 64
    assert result.events_url == f"/api/v1/runs/{RUN_ID}/events"
    assert result.stream_url.endswith("/events/stream")


@pytest.mark.asyncio
async def test_list_requires_session_read_and_run_list() -> None:
    stub = Stub(access("run:list"))
    with pytest.raises(PlatformError) as raised:
        await service(stub).list_session_runs(
            principal(),
            session_id=str(SESSION_ID),
            limit=20,
            cursor=None,
            metadata=METADATA,
        )
    assert raised.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_get_run_returns_frozen_snapshot_and_deployment() -> None:
    stub = Stub(access("run:read"))
    result = await service(stub).get_run(
        principal(), run_id=str(RUN_ID), metadata=METADATA
    )

    assert result.snapshot_id == str(SNAPSHOT_ID)
    assert result.deployment_id == str(DEPLOYMENT_ID)
    assert result.status == "CREATED"


@pytest.mark.asyncio
async def test_cancel_signals_bound_workflow_and_returns_cancelling() -> None:
    stub = Stub(access("run:cancel"))
    stub.current_record = replace(record(), workflow_id=f"run/{TENANT_ID}/{RUN_ID}")
    control = WorkflowControlStub()

    result = await service(stub, control).cancel_run(
        principal(),
        run_id=str(RUN_ID),
        request=CancelRunRequest(reason="user stop"),
        idempotency_key="cancel-key-001",
        metadata=METADATA,
    )

    assert result.status == "CANCELLING"
    assert len(control.signals) == 1
    assert control.signals[0].reason == "user stop"


@pytest.mark.asyncio
async def test_retry_returns_new_run_linked_to_original() -> None:
    stub = Stub(access("run:retry"))

    result = await service(stub).retry_run(
        principal(),
        run_id=str(RUN_ID),
        request=RetryRunRequest(deployment_policy="original_snapshot"),
        idempotency_key="retry-key-001",
        metadata=METADATA,
    )

    assert result.run_id == "99999999-9999-4999-8999-999999999999"
    assert stub.request_hash is not None and len(stub.request_hash) == 64
