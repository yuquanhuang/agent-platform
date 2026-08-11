"""AgentScope control-event bridge for approval-gated external tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from agentscope.event import (
    AgentEvent,
    ConfirmResult,
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState
from pydantic import JsonValue

from packages.application.approvals import ApprovalCoordinator, ApprovalRequestInput
from packages.application.metadata import RequestMetadata
from packages.application.temporal.run_activities import (
    RunExecutionRequest,
    RunRuntimeError,
    RuntimeEventCandidatePublisher,
)
from packages.application.tool_gateway import (
    ExecutionTicketIssuer,
    ToolExecutionDenied,
    ToolExecutionRequest,
    ToolGatewayService,
    canonical_tool_parameter_digest,
    execution_ticket_ref,
)
from packages.contracts.public import TenantContext
from packages.contracts.temporal import (
    AssistantContentPart,
    AssistantTextPart,
    AssistantToolReferencePart,
    RuntimeCompletion,
)
from packages.domain.approvals import ApprovalRequestRecord
from packages.domain.execution_tickets import ExecutionTicketRecord
from packages.runtimes.agentscope.events import AgentScopeEventTranslator

_MAX_STATE_BYTES = 10_485_760
_MAX_TOOL_ARGUMENT_BYTES = 1_048_576
_MAX_TOOL_RESULT_BYTES = 1_048_576


class AgentScopeReplySession(Protocol):
    """Narrow AgentScope session surface retained inside the runtime package."""

    state: AgentState

    def reply_stream(
        self,
        inputs: (
            Msg
            | list[Msg]
            | UserConfirmResultEvent
            | ExternalExecutionResultEvent
            | None
        ) = None,
        *,
        yield_final_msg: bool = False,
    ) -> AsyncIterator[AgentEvent | Msg]: ...


@dataclass(frozen=True, slots=True)
class AgentScopeSessionStart:
    session: AgentScopeReplySession
    initial_input: Msg | list[Msg] | None


class AgentScopeSessionFactory(Protocol):
    """Build one session exclusively from an immutable RunSpec reference."""

    async def create(
        self, context: TenantContext, *, request: RunExecutionRequest
    ) -> AgentScopeSessionStart: ...


class AgentScopeStateStore(Protocol):
    """Persist sensitive runtime state outside Temporal Workflow History."""

    async def save(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        state_json: bytes,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class RuntimeToolBinding:
    """Trusted immutable facts for one proposed AgentScope tool call."""

    tool_call_id: str
    requester_id: UUID
    deployment_id: UUID
    tool_name: str
    tool_schema_hash: str
    policy_version: str
    arguments: dict[str, JsonValue]
    risk_level: Literal["HIGH"]
    approval_expires_at: datetime
    self_approval_allowed: bool = False


class RuntimeToolBindingResolver(Protocol):
    """Validate a model proposal against RunSpec, Bundle and registry facts."""

    async def resolve(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        tool_call: ToolCallBlock,
    ) -> RuntimeToolBinding: ...


class RuntimeApprovalStore(Protocol):
    async def get_approval(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        now: datetime,
    ) -> ApprovalRequestRecord | None: ...

    async def get_ticket_for_approval(
        self, context: TenantContext, *, approval_id: UUID
    ) -> ExecutionTicketRecord | None: ...


class AgentScopeApprovalBridge:
    """Compose the existing ApprovalCoordinator and durable Approval store."""

    def __init__(
        self,
        coordinator: ApprovalCoordinator,
        store: RuntimeApprovalStore,
    ) -> None:
        self._coordinator = coordinator
        self._store = store

    async def request(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        binding: RuntimeToolBinding,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ApprovalRequestRecord:
        return await self._coordinator.request_approval(
            context,
            request=ApprovalRequestInput(
                run_id=request.run_id,
                execution_attempt=request.execution_attempt,
                requester_id=binding.requester_id,
                tool_call_id=binding.tool_call_id,
                tool_name=binding.tool_name,
                tool_schema_hash=binding.tool_schema_hash,
                parameter_digest=canonical_tool_parameter_digest(binding.arguments),
                policy_version=binding.policy_version,
                deployment_id=binding.deployment_id,
                expires_at=binding.approval_expires_at,
                self_approval_allowed=binding.self_approval_allowed,
            ),
            metadata=metadata,
            now=now,
        )

    async def get(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        now: datetime,
    ) -> ApprovalRequestRecord | None:
        return await self._store.get_approval(context, approval_id=approval_id, now=now)

    async def get_ticket(
        self, context: TenantContext, *, approval_id: UUID
    ) -> ExecutionTicketRecord | None:
        return await self._store.get_ticket_for_approval(
            context, approval_id=approval_id
        )


class AgentScopeRuntimeBridgeError(RunRuntimeError):
    """Safe stable failure emitted by the runtime control bridge."""


@dataclass(frozen=True, slots=True)
class _ApprovedToolCall:
    approval: ApprovalRequestRecord
    binding: RuntimeToolBinding
    ticket: ExecutionTicketRecord


class AgentScopeRuntimeBridge:
    """Execute AgentScope while parking high-risk calls behind platform facts."""

    def __init__(
        self,
        session_factory: AgentScopeSessionFactory,
        state_store: AgentScopeStateStore,
        binding_resolver: RuntimeToolBindingResolver,
        approvals: AgentScopeApprovalBridge,
        ticket_issuer: ExecutionTicketIssuer,
        tool_gateway: ToolGatewayService,
        *,
        approval_poll_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if approval_poll_seconds <= 0:
            raise ValueError("approval_poll_seconds must be positive")
        self._session_factory = session_factory
        self._state_store = state_store
        self._binding_resolver = binding_resolver
        self._approvals = approvals
        self._ticket_issuer = ticket_issuer
        self._tool_gateway = tool_gateway
        self._approval_poll_seconds = approval_poll_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        event_publisher: RuntimeEventCandidatePublisher,
    ) -> RuntimeCompletion:
        if request.runtime_type != "agentscope":
            raise AgentScopeRuntimeBridgeError(
                "RUNTIME_TYPE_MISMATCH",
                "The AgentScope bridge cannot execute another runtime type.",
            )
        start = await self._session_factory.create(context, request=request)
        translator = AgentScopeEventTranslator()
        metadata = RequestMetadata(
            request_id=context.request_id,
            trace_id=context.trace_id,
        )
        approved: dict[str, _ApprovedToolCall] = {}
        text_parts: list[str] = []
        tool_references: list[str] = []
        next_input: (
            Msg
            | list[Msg]
            | UserConfirmResultEvent
            | ExternalExecutionResultEvent
            | None
        ) = start.initial_input

        while True:
            control_input = None
            completed = False
            async for item in start.session.reply_stream(
                next_input, yield_final_msg=False
            ):
                if isinstance(item, Msg):
                    raise AgentScopeRuntimeBridgeError(
                        "AGENTSCOPE_FINAL_MESSAGE_DIRECTION_INVALID",
                        "AgentScope returned an unexpected final message object.",
                    )
                if isinstance(item, RequireUserConfirmEvent):
                    control_input = await self._approve(
                        context,
                        request=request,
                        session=start.session,
                        event=item,
                        metadata=metadata,
                        approved=approved,
                    )
                    break
                if isinstance(item, RequireExternalExecutionEvent):
                    control_input = await self._execute_external(
                        context,
                        request=request,
                        event=item,
                        metadata=metadata,
                        approved=approved,
                    )
                    break
                candidate = translator.translate(item)
                if candidate is not None:
                    await event_publisher.publish(
                        context, request=request, candidate=candidate
                    )
                    if candidate.event_type == "text_delta":
                        text_parts.append(candidate.payload.delta)
                    elif candidate.event_type == "tool_call_start":
                        tool_call_id = str(candidate.payload.tool_call_id)
                        if tool_call_id not in tool_references:
                            tool_references.append(tool_call_id)
                if isinstance(item, ReplyEndEvent):
                    if str(item.finished_reason) == "error":
                        raise AgentScopeRuntimeBridgeError(
                            "AGENTSCOPE_REPLY_FAILED",
                            "AgentScope ended the reply with an error.",
                        )
                    completed = True
            if control_input is not None:
                next_input = control_input
                continue
            if completed:
                return _completion(
                    start.session.state.session_id,
                    text_parts=text_parts,
                    tool_references=tool_references,
                )
            raise AgentScopeRuntimeBridgeError(
                "AGENTSCOPE_STREAM_INCOMPLETE",
                "AgentScope ended without a terminal reply or control request.",
            )

    async def _approve(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        session: AgentScopeReplySession,
        event: RequireUserConfirmEvent,
        metadata: RequestMetadata,
        approved: dict[str, _ApprovedToolCall],
    ) -> UserConfirmResultEvent:
        tool_call = _single_tool_call(event.tool_calls)
        binding = await self._binding_resolver.resolve(
            context, request=request, tool_call=tool_call
        )
        _validate_binding(binding, tool_call)
        state_json = session.state.model_dump_json().encode()
        if len(state_json) > _MAX_STATE_BYTES:
            raise AgentScopeRuntimeBridgeError(
                "AGENTSCOPE_STATE_TOO_LARGE",
                "The AgentScope checkpoint exceeds the runtime state limit.",
            )
        state_ref = await self._state_store.save(
            context, request=request, state_json=state_json
        )
        if not state_ref or len(state_ref) > 2048:
            raise AgentScopeRuntimeBridgeError(
                "AGENTSCOPE_STATE_REF_INVALID",
                "The AgentScope checkpoint reference is invalid.",
            )
        approval = await self._approvals.request(
            context,
            request=request,
            binding=binding,
            metadata=metadata,
            now=self._clock(),
        )
        resolution = await self._wait_for_approval(
            context, request=request, approval=approval
        )
        if resolution.status != "APPROVED":
            code = {
                "REJECTED": "RUN_CANCELLING",
                "CANCELLED": "RUN_CANCELLING",
                "EXPIRED": "APPROVAL_EXPIRED",
            }.get(resolution.status, "APPROVAL_NOT_APPROVED")
            raise AgentScopeRuntimeBridgeError(
                code,
                "The high-risk tool call was not approved.",
            )
        ticket = await self._approvals.get_ticket(context, approval_id=resolution.id)
        if ticket is None:
            raise AgentScopeRuntimeBridgeError(
                "EXECUTION_TICKET_UNAVAILABLE",
                "The approved tool call has no Execution Ticket.",
            )
        credential = self._ticket_issuer.issue(
            tenant_id=resolution.tenant_id, approval_id=resolution.id
        )
        if (
            ticket.id != credential.ticket_id
            or ticket.nonce_hash != credential.nonce_hash
            or execution_ticket_ref(ticket.id) != credential.ticket_ref
        ):
            raise AgentScopeRuntimeBridgeError(
                "EXECUTION_TICKET_DERIVATION_MISMATCH",
                "The Execution Ticket credential cannot be reconstructed safely.",
            )
        approved[tool_call.id] = _ApprovedToolCall(
            approval=resolution,
            binding=binding,
            ticket=ticket,
        )
        return UserConfirmResultEvent(
            reply_id=event.reply_id,
            confirm_results=[
                ConfirmResult(
                    confirmed=True,
                    tool_call=ToolCallBlock(
                        id=binding.tool_call_id,
                        name=binding.tool_name,
                        input=_canonical_arguments(binding.arguments),
                    ),
                )
            ],
        )

    async def _wait_for_approval(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        approval: ApprovalRequestRecord,
    ) -> ApprovalRequestRecord:
        current = approval
        while current.status == "PENDING":
            if request.heartbeat is not None:
                request.heartbeat("waiting_approval")
            await asyncio.sleep(self._approval_poll_seconds)
            loaded = await self._approvals.get(
                context,
                approval_id=approval.id,
                now=self._clock(),
            )
            if loaded is None:
                raise AgentScopeRuntimeBridgeError(
                    "APPROVAL_NOT_FOUND",
                    "The durable Approval request is unavailable.",
                )
            if loaded.status == "PENDING" and self._clock() >= loaded.expires_at:
                raise AgentScopeRuntimeBridgeError(
                    "APPROVAL_EXPIRY_NOT_COMMITTED",
                    "The expired Approval did not converge to a terminal fact.",
                )
            current = loaded
        return current

    async def _execute_external(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        event: RequireExternalExecutionEvent,
        metadata: RequestMetadata,
        approved: dict[str, _ApprovedToolCall],
    ) -> ExternalExecutionResultEvent:
        tool_call = _single_tool_call(event.tool_calls)
        authorization = approved.pop(tool_call.id, None)
        if authorization is None:
            raise AgentScopeRuntimeBridgeError(
                "PLATFORM_EXTERNAL_EXECUTION_APPROVAL_REQUIRED",
                "External tool execution requires an approved platform ticket.",
            )
        current = await self._binding_resolver.resolve(
            context, request=request, tool_call=tool_call
        )
        _validate_binding(current, tool_call)
        if current != authorization.binding:
            raise AgentScopeRuntimeBridgeError(
                "RUNTIME_TOOL_BINDING_CHANGED",
                "The tool binding changed after approval.",
            )
        credential = self._ticket_issuer.issue(
            tenant_id=authorization.approval.tenant_id,
            approval_id=authorization.approval.id,
        )
        try:
            result = await self._tool_gateway.execute(
                context,
                request=ToolExecutionRequest(
                    tenant_id=request.tenant_id,
                    run_id=request.run_id,
                    execution_attempt=request.execution_attempt,
                    approval_id=authorization.approval.id,
                    requester_id=current.requester_id,
                    deployment_id=current.deployment_id,
                    ticket_ref=credential.ticket_ref,
                    ticket_nonce=credential.nonce,
                    tool_name=current.tool_name,
                    tool_schema_hash=current.tool_schema_hash,
                    parameter_digest=canonical_tool_parameter_digest(current.arguments),
                    policy_version=current.policy_version,
                    arguments=current.arguments,
                ),
                metadata=metadata,
                now=self._clock(),
            )
        except ToolExecutionDenied as error:
            raise AgentScopeRuntimeBridgeError(
                error.code,
                "The Tool Gateway rejected the approved tool call.",
            ) from error
        except Exception as error:
            raise AgentScopeRuntimeBridgeError(
                "TOOL_EXECUTION_FAILED",
                "The external tool execution failed after Ticket consumption.",
            ) from error
        if request.heartbeat is not None:
            request.heartbeat("external_execution_completed")
        output = (
            _json_output(result.output)
            if result.status == "SUCCEEDED"
            else _json_output({"error_code": result.error_code or "TOOL_FAILED"})
        )
        return ExternalExecutionResultEvent(
            reply_id=event.reply_id,
            execution_results=[
                ToolResultBlock(
                    id=current.tool_call_id,
                    name=current.tool_name,
                    output=output,
                    state=(
                        ToolResultState.SUCCESS
                        if result.status == "SUCCEEDED"
                        else ToolResultState.ERROR
                    ),
                )
            ],
        )


def _single_tool_call(tool_calls: list[ToolCallBlock]) -> ToolCallBlock:
    if len(tool_calls) != 1:
        raise AgentScopeRuntimeBridgeError(
            "AGENTSCOPE_TOOL_BATCH_UNSUPPORTED",
            "Approval-gated execution requires exactly one tool call.",
        )
    return tool_calls[0]


def _validate_binding(binding: RuntimeToolBinding, tool_call: ToolCallBlock) -> None:
    if binding.risk_level != "HIGH":
        raise AgentScopeRuntimeBridgeError(
            "RUNTIME_TOOL_RISK_INVALID",
            "The approval bridge accepts only high-risk tool bindings.",
        )
    if binding.tool_call_id != tool_call.id or binding.tool_name != tool_call.name:
        raise AgentScopeRuntimeBridgeError(
            "RUNTIME_TOOL_IDENTITY_MISMATCH",
            "The trusted tool binding does not match the AgentScope proposal.",
        )
    if binding.approval_expires_at.tzinfo is None:
        raise AgentScopeRuntimeBridgeError(
            "APPROVAL_EXPIRY_INVALID",
            "Approval expiry must include a timezone.",
        )
    if _canonical_arguments(binding.arguments) != tool_call.input:
        raise AgentScopeRuntimeBridgeError(
            "RUNTIME_TOOL_ARGUMENTS_MISMATCH",
            "The trusted tool arguments do not match the AgentScope proposal.",
        )


def _canonical_arguments(arguments: dict[str, JsonValue]) -> str:
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode()) > _MAX_TOOL_ARGUMENT_BYTES:
        raise AgentScopeRuntimeBridgeError(
            "RUNTIME_TOOL_ARGUMENTS_TOO_LARGE",
            "The tool arguments exceed the runtime bridge limit.",
        )
    return encoded


def _json_output(value: JsonValue | None) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode()) > _MAX_TOOL_RESULT_BYTES:
        raise AgentScopeRuntimeBridgeError(
            "TOOL_RESULT_TOO_LARGE",
            "The external tool result exceeds the AgentScope bridge limit.",
        )
    return encoded


def _completion(
    session_id: str,
    *,
    text_parts: list[str],
    tool_references: list[str],
) -> RuntimeCompletion:
    content: list[AssistantContentPart] = []
    text = "".join(text_parts)
    if text:
        content.append(AssistantTextPart(text=text))
    content.extend(
        AssistantToolReferencePart(tool_call_id=tool_call_id)
        for tool_call_id in tool_references
    )
    if not content:
        raise AgentScopeRuntimeBridgeError(
            "AGENTSCOPE_EMPTY_COMPLETION",
            "AgentScope completed without assistant content.",
        )
    return RuntimeCompletion(
        status="SUCCEEDED",
        assistant_content_parts=tuple(content),
        result_quality="NORMAL",
        runtime_handle_ref=f"agentscope://session/{session_id}",
    )
