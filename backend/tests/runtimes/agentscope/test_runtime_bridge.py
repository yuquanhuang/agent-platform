"""AgentScope approval, Ticket, external execution and resume bridge tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from agentscope.event import (
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.types import ReplyFinishedReason

from packages.application.temporal import RunExecutionRequest
from packages.application.tool_gateway import ToolExecutionResult
from packages.contracts.generated.run_event import RuntimeEventCandidate
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import RunSpecReference
from packages.domain.approvals import ApprovalRequestRecord
from packages.domain.execution_tickets import ExecutionTicketRecord
from packages.infrastructure.tool_gateway import HmacExecutionTicketIssuer
from packages.runtimes.agentscope import (
    AgentScopeRuntimeBridge,
    AgentScopeRuntimeBridgeError,
    AgentScopeSessionStart,
    RuntimeToolBinding,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUESTER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
DEPLOYMENT_ID = UUID("44444444-4444-4444-8444-444444444444")
APPROVAL_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(RUN_ID),
        auth_time=NOW,
        request_id="request-runtime-bridge",
        trace_id="trace-runtime-bridge",
    )


def execution_request(heartbeats: list[str] | None = None) -> RunExecutionRequest:
    return RunExecutionRequest(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        run_spec=RunSpecReference(
            uri="memory://run-spec/1",
            content_hash="sha256:" + "a" * 64,
            size_bytes=1024,
        ),
        timeout_seconds=600,
        runtime_type="agentscope",
        fencing_token=__import__("pydantic").SecretStr("f" * 32),
        heartbeat=(heartbeats.append if heartbeats is not None else None),
    )


def binding() -> RuntimeToolBinding:
    return RuntimeToolBinding(
        tool_call_id="tool-1",
        requester_id=REQUESTER_ID,
        deployment_id=DEPLOYMENT_ID,
        tool_name="production.write",
        tool_schema_hash="sha256:" + "b" * 64,
        policy_version="policy/v1",
        arguments={"resource": "prod"},
        risk_level="HIGH",
        approval_expires_at=NOW + timedelta(minutes=5),
    )


def approval(status: str = "PENDING") -> ApprovalRequestRecord:
    return ApprovalRequestRecord(
        id=APPROVAL_ID,
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        requester_id=REQUESTER_ID,
        tool_call_id="tool-1",
        tool_name="production.write",
        tool_schema_hash="sha256:" + "b" * 64,
        parameter_digest=(
            "sha256:0c0e5ad9035ce4798240e15bf8b3e087bf1a7d0d55f703d0c5a3e756a7dd330b"
        ),
        policy_version="policy/v1",
        deployment_id=DEPLOYMENT_ID,
        status=cast(object, status),  # type: ignore[arg-type]
        expires_at=NOW + timedelta(minutes=5),
        resource_version=1 if status == "PENDING" else 2,
        self_approval_allowed=False,
        created_at=NOW,
        updated_at=NOW,
    )


class Session:
    def __init__(self, *, external_first: bool = False) -> None:
        self.state = AgentState(session_id="agent-session-1")
        self.external_first = external_first
        self.inputs: list[object] = []

    async def reply_stream(
        self, inputs: object = None, *, yield_final_msg: bool = False
    ):
        assert yield_final_msg is False
        self.inputs.append(inputs)
        call = ToolCallBlock(
            id="tool-1", name="production.write", input='{"resource":"prod"}'
        )
        if len(self.inputs) == 1:
            yield ReplyStartEvent(
                id="reply-start",
                session_id="agent-session-1",
                reply_id="reply-1",
                name="assistant",
            )
            yield ToolCallStartEvent(
                id="tool-start",
                reply_id="reply-1",
                tool_call_id="tool-1",
                tool_call_name="production.write",
            )
            if self.external_first:
                yield RequireExternalExecutionEvent(
                    reply_id="reply-1", tool_calls=[call]
                )
            else:
                yield RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[call])
            return
        if isinstance(inputs, UserConfirmResultEvent):
            assert inputs.confirm_results[0].confirmed is True
            yield RequireExternalExecutionEvent(reply_id="reply-1", tool_calls=[call])
            return
        assert isinstance(inputs, ExternalExecutionResultEvent)
        assert inputs.execution_results[0].state == ToolResultState.SUCCESS
        yield ToolResultStartEvent(
            id="result-start",
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_call_name="production.write",
        )
        yield ToolResultTextDeltaEvent(
            id="result-delta",
            reply_id="reply-1",
            tool_call_id="tool-1",
            delta='{"ok":true}',
        )
        yield ToolResultEndEvent(
            id="result-end",
            reply_id="reply-1",
            tool_call_id="tool-1",
            state=ToolResultState.SUCCESS,
        )
        yield TextBlockDeltaEvent(
            id="text-delta",
            reply_id="reply-1",
            block_id="text-1",
            delta="done",
        )
        yield ReplyEndEvent(
            id="reply-end",
            session_id="agent-session-1",
            reply_id="reply-1",
            finished_reason=ReplyFinishedReason.COMPLETED,
        )


class Stub:
    def __init__(self, *, decision: str = "APPROVED", external_first: bool = False):
        self.session = Session(external_first=external_first)
        self.decision = decision
        self.saved_states: list[bytes] = []
        self.published: list[RuntimeEventCandidate] = []
        self.gateway_requests: list[object] = []
        self.requested = 0
        self.issuer = HmacExecutionTicketIssuer(
            __import__("pydantic").SecretStr("ticket-secret-" * 4)
        )

    async def create(self, context: TenantContext, *, request: RunExecutionRequest):
        return AgentScopeSessionStart(session=self.session, initial_input=None)

    async def save(self, context: TenantContext, **kwargs: object) -> str:
        self.saved_states.append(cast(bytes, kwargs["state_json"]))
        return "state://tenant/run/attempt/checkpoint-1"

    async def resolve(self, context: TenantContext, **kwargs: object):
        return binding()

    async def request(self, context: TenantContext, **kwargs: object):
        self.requested += 1
        return approval()

    async def get(self, context: TenantContext, **kwargs: object):
        return approval(self.decision)

    async def get_ticket(self, context: TenantContext, **kwargs: object):
        credential = self.issuer.issue(tenant_id=TENANT_ID, approval_id=APPROVAL_ID)
        return ExecutionTicketRecord(
            id=credential.ticket_id,
            tenant_id=TENANT_ID,
            approval_id=APPROVAL_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            requester_id=REQUESTER_ID,
            tool_name="production.write",
            tool_schema_hash="sha256:" + "b" * 64,
            parameter_digest=approval().parameter_digest,
            policy_version="policy/v1",
            deployment_id=DEPLOYMENT_ID,
            nonce_hash=credential.nonce_hash,
            expires_at=NOW + timedelta(minutes=5),
            single_use=True,
            consumed_at=None,
            created_at=NOW,
        )

    async def execute(self, context: TenantContext, **kwargs: object):
        self.gateway_requests.append(kwargs["request"])
        return ToolExecutionResult(status="SUCCEEDED", output={"ok": True})

    async def publish(self, context: TenantContext, **kwargs: object) -> None:
        self.published.append(cast(RuntimeEventCandidate, kwargs["candidate"]))


def bridge(stub: Stub) -> AgentScopeRuntimeBridge:
    return AgentScopeRuntimeBridge(
        stub,
        stub,
        stub,
        cast(object, stub),  # type: ignore[arg-type]
        stub.issuer,
        cast(object, stub),  # type: ignore[arg-type]
        approval_poll_seconds=0.001,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_bridge_parks_approves_executes_once_and_resumes_agentscope() -> None:
    stub = Stub()
    heartbeats: list[str] = []

    result = await bridge(stub).execute(
        context(),
        request=execution_request(heartbeats),
        event_publisher=stub,
    )

    assert result.status == "SUCCEEDED"
    assert result.runtime_handle_ref == "agentscope://session/agent-session-1"
    assert result.assistant_content_parts[0].text == "done"  # type: ignore[union-attr]
    assert stub.requested == 1
    assert len(stub.saved_states) == 1
    assert b"agent-session-1" in stub.saved_states[0]
    assert len(stub.gateway_requests) == 1
    assert heartbeats == ["waiting_approval", "external_execution_completed"]
    assert [candidate.event_type for candidate in stub.published] == [
        "text_message_start",
        "tool_call_start",
        "tool_call_result",
        "text_delta",
        "text_message_end",
    ]


@pytest.mark.asyncio
async def test_bridge_rejection_never_calls_tool_gateway() -> None:
    stub = Stub(decision="REJECTED")

    with pytest.raises(AgentScopeRuntimeBridgeError) as raised:
        await bridge(stub).execute(
            context(), request=execution_request(), event_publisher=stub
        )

    assert raised.value.code == "RUN_CANCELLING"
    assert stub.gateway_requests == []


@pytest.mark.asyncio
async def test_bridge_fails_closed_for_external_execution_without_approval() -> None:
    stub = Stub(external_first=True)

    with pytest.raises(AgentScopeRuntimeBridgeError) as raised:
        await bridge(stub).execute(
            context(), request=execution_request(), event_publisher=stub
        )

    assert raised.value.code == "PLATFORM_EXTERNAL_EXECUTION_APPROVAL_REQUIRED"
    assert stub.requested == 0
    assert stub.gateway_requests == []
