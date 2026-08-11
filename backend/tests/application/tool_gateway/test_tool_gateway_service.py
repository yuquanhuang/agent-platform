"""Tool Gateway authorization, single-consumption ordering and denial tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.application.metadata import RequestMetadata
from packages.application.tool_gateway import (
    ToolAuthorizationContext,
    ToolExecutionDenied,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolGatewayService,
    canonical_tool_parameter_digest,
    execution_ticket_ref,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.execution_tickets import ExecutionTicketRecord

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
APPROVAL_ID = UUID("44444444-4444-4444-8444-444444444444")
DEPLOYMENT_ID = UUID("55555555-5555-4555-8555-555555555555")
TICKET_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(USER_ID),
        membership_version=1,
        auth_time=NOW,
        request_id="request-1",
        trace_id="trace-1",
    )


def request(**changes: object) -> ToolExecutionRequest:
    value = ToolExecutionRequest(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        approval_id=APPROVAL_ID,
        requester_id=USER_ID,
        deployment_id=DEPLOYMENT_ID,
        ticket_ref=execution_ticket_ref(TICKET_ID),
        ticket_nonce=SecretStr("n" * 43),
        tool_name="production.write",
        tool_schema_hash="sha256:" + "a" * 64,
        parameter_digest=canonical_tool_parameter_digest({"resource": "redacted"}),
        policy_version="policy/v1",
        arguments={"resource": "redacted"},
    )
    return replace(value, **changes)


class Stub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.authorization = ToolAuthorizationContext(
            permission_allowed=True,
            policy_allowed=True,
            tool_name="production.write",
            tool_schema_hash="sha256:" + "a" * 64,
            policy_version="policy/v1",
            deployment_id=DEPLOYMENT_ID,
        )

    async def resolve(self, context: TenantContext, *, request: ToolExecutionRequest):
        self.calls.append("authorize")
        return self.authorization

    async def consume(self, context: TenantContext, **kwargs: object):
        self.calls.append("consume")
        return ExecutionTicketRecord(
            id=TICKET_ID,
            tenant_id=TENANT_ID,
            approval_id=APPROVAL_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            requester_id=USER_ID,
            tool_name="production.write",
            tool_schema_hash="sha256:" + "a" * 64,
            parameter_digest=canonical_tool_parameter_digest({"resource": "redacted"}),
            policy_version="policy/v1",
            deployment_id=DEPLOYMENT_ID,
            nonce_hash="sha256:" + "c" * 64,
            expires_at=NOW + timedelta(minutes=5),
            single_use=True,
            consumed_at=NOW,
            created_at=NOW,
        )

    async def execute(self, context: TenantContext, *, request: ToolExecutionRequest):
        self.calls.append("execute")
        return ToolExecutionResult(status="SUCCEEDED", output={"ok": True})

    async def audit_tool_result(self, context: TenantContext, **kwargs: object) -> None:
        self.calls.append("audit-result")

    async def audit_denied(self, context: TenantContext, **kwargs: object) -> None:
        self.calls.append("audit-denied")


@pytest.mark.asyncio
async def test_gateway_consumes_before_external_execution() -> None:
    stub = Stub()
    service = ToolGatewayService(stub, stub, stub)

    result = await service.execute(
        context(),
        request=request(),
        metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
        now=NOW,
    )

    assert result.status == "SUCCEEDED"
    assert stub.calls == ["authorize", "consume", "execute", "audit-result"]


@pytest.mark.asyncio
async def test_gateway_accepts_runtime_service_bound_to_same_run() -> None:
    stub = Stub()
    service = ToolGatewayService(stub, stub, stub)
    runtime_context = context().model_copy(
        update={
            "subject_type": SubjectType.SERVICE,
            "subject_id": str(RUN_ID),
            "membership_version": None,
        }
    )

    result = await service.execute(
        runtime_context,
        request=request(),
        metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
        now=NOW,
    )

    assert result.status == "SUCCEEDED"
    assert stub.calls == ["authorize", "consume", "execute", "audit-result"]


@pytest.mark.asyncio
async def test_gateway_rejects_runtime_service_bound_to_another_run() -> None:
    stub = Stub()
    service = ToolGatewayService(stub, stub, stub)
    runtime_context = context().model_copy(
        update={
            "subject_type": SubjectType.SERVICE,
            "subject_id": str(UUID("88888888-8888-4888-8888-888888888888")),
            "membership_version": None,
        }
    )

    with pytest.raises(ToolExecutionDenied) as raised:
        await service.execute(
            runtime_context,
            request=request(),
            metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
            now=NOW,
        )

    assert raised.value.code == "EXECUTION_TICKET_REQUESTER_MISMATCH"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_gateway_denies_changed_schema_without_consuming() -> None:
    stub = Stub()
    stub.authorization = replace(
        stub.authorization, tool_schema_hash="sha256:" + "d" * 64
    )
    service = ToolGatewayService(stub, stub, stub)

    with pytest.raises(ToolExecutionDenied) as raised:
        await service.execute(
            context(),
            request=request(),
            metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
            now=NOW,
        )

    assert raised.value.code == "TOOL_SCHEMA_CHANGED"
    assert stub.calls == ["authorize", "audit-denied"]


@pytest.mark.asyncio
async def test_gateway_rejects_cross_tenant_request_before_authorization() -> None:
    stub = Stub()
    service = ToolGatewayService(stub, stub, stub)

    with pytest.raises(ToolExecutionDenied) as raised:
        await service.execute(
            context(),
            request=request(tenant_id=UUID("77777777-7777-4777-8777-777777777777")),
            metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
            now=NOW,
        )

    assert raised.value.code == "EXECUTION_TICKET_TENANT_MISMATCH"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_gateway_rejects_arguments_changed_after_approval() -> None:
    stub = Stub()
    service = ToolGatewayService(stub, stub, stub)

    with pytest.raises(ToolExecutionDenied) as raised:
        await service.execute(
            context(),
            request=request(arguments={"resource": "changed"}),
            metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
            now=NOW,
        )

    assert raised.value.code == "EXECUTION_TICKET_PARAMETER_DIGEST_INVALID"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_gateway_audits_executor_failure_after_ticket_consumption() -> None:
    stub = Stub()

    async def fail(context: TenantContext, *, request: ToolExecutionRequest):
        raise RuntimeError("external tool failed")

    stub.execute = fail  # type: ignore[method-assign]
    service = ToolGatewayService(stub, stub, stub)

    with pytest.raises(RuntimeError, match="external tool failed"):
        await service.execute(
            context(),
            request=request(),
            metadata=RequestMetadata(request_id="request-1", trace_id="trace-1"),
            now=NOW,
        )

    assert stub.calls == ["authorize", "consume", "audit-result"]
