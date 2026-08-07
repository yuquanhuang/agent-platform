import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, TypedDict, cast
from uuid import uuid4

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import ToolChoice

from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ModelGatewayStreamEvent,
    ModelUsage,
    ResponseCompletedEvent,
    ResponseCompletedPayload,
    ResponseErrorEvent,
    ResponseErrorPayload,
    ResponseStartedEvent,
    ResponseStartedPayload,
    TextDeltaEvent,
    TextDeltaPayload,
    ToolCallDeltaEvent,
    ToolCallDeltaPayload,
    UsageEvent,
    UsagePayload,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.runtimes.agentscope.model_bridge import (
    AgentScopeGatewayChatModel,
    ModelGatewayBridgeError,
    ModelParameter,
)


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(uuid4()),
        subject_type=SubjectType.USER,
        subject_id=str(uuid4()),
        membership_version=1,
        auth_time=datetime.now(UTC),
        request_id="request-1",
        trace_id="trace-1",
    )


class FakeInvocationWriter:
    def __init__(self, context: TenantContext) -> None:
        self.context = context
        self.calls = 0
        self.messages: list[Msg] = []
        self.parameters: dict[str, ModelParameter] = {}

    async def create_request(
        self,
        context: TenantContext,
        messages: list[Msg],
        tools: list[dict[str, Any]] | None,
        tool_choice: ToolChoice | None,
        parameters: Mapping[str, ModelParameter],
    ) -> ModelGatewayRequest:
        del tools, tool_choice
        self.calls += 1
        self.messages = messages
        self.parameters = dict(parameters)
        return ModelGatewayRequest(
            schema_version="1.0",
            tenant_id=context.tenant_id,
            user_id=context.subject_id,
            agent_id="agent-1",
            snapshot_id="snapshot-1",
            run_id="run-1",
            execution_attempt=1,
            model_binding_id="binding-1",
            prompt_ref=ImmutableReference(
                uri="artifact://prompt/1", hash=f"sha256:{'a' * 64}"
            ),
            tools_ref=ImmutableReference(
                uri="artifact://tools/1", hash=f"sha256:{'b' * 64}"
            ),
            capability_requirements=["stream", "tools"],
            parameters=dict(parameters),
            stream=True,
            timeout_seconds=30,
            idempotency_key="runtime-call-0001",
            authorization_token="runtime-authorization-token",
        )


class FakeGateway:
    def __init__(self, events: list[ModelGatewayStreamEvent]) -> None:
        self.events = events
        self.calls = 0

    async def stream(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> AsyncIterator[ModelGatewayStreamEvent]:
        del context, request
        self.calls += 1
        for event in self.events:
            yield event


class CancellingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> AsyncIterator[ModelGatewayStreamEvent]:
        del context, request
        self.calls += 1
        raise asyncio.CancelledError
        yield  # pragma: no cover


class _EventBase(TypedDict):
    event_id: str
    provider: str
    model: str
    provider_request_id: str | None
    occurred_at: datetime


def _event_base(event_id: str) -> _EventBase:
    return {
        "event_id": event_id,
        "provider": "openai",
        "model": "gpt-test",
        "provider_request_id": "provider-request-1",
        "occurred_at": datetime.now(UTC),
    }


def _success_events() -> list[ModelGatewayStreamEvent]:
    usage = ModelUsage(
        input_tokens=10,
        output_tokens=4,
        reasoning_tokens=1,
        cache_read_tokens=2,
        cache_write_tokens=3,
        estimated=False,
    )
    output_ref = ImmutableReference(
        uri="artifact://model-output/1", hash=f"sha256:{'c' * 64}"
    )
    events: list[ModelGatewayStreamEvent] = [
        ResponseStartedEvent(
            **_event_base("gateway-start"),
            event_type="response_started",
            payload=ResponseStartedPayload(),
        ),
        TextDeltaEvent(
            **_event_base("gateway-text"),
            event_type="text_delta",
            payload=TextDeltaPayload(delta="hello"),
        ),
        ToolCallDeltaEvent(
            **_event_base("gateway-tool-1"),
            event_type="tool_call_delta",
            payload=ToolCallDeltaPayload(
                tool_call_id="tool-1",
                tool_name="search",
                arguments_patch='{"query":',
            ),
        ),
        ToolCallDeltaEvent(
            **_event_base("gateway-tool-2"),
            event_type="tool_call_delta",
            payload=ToolCallDeltaPayload(
                tool_call_id="tool-1",
                tool_name=None,
                arguments_patch='"agent"}',
            ),
        ),
        UsageEvent(
            **_event_base("gateway-usage"),
            event_type="usage",
            payload=UsagePayload(usage=usage),
        ),
        ResponseCompletedEvent(
            **_event_base("gateway-complete"),
            event_type="response_completed",
            payload=ResponseCompletedPayload(
                finish_reason="tool_call", output_ref=output_ref
            ),
        ),
    ]
    return events


@pytest.mark.asyncio
async def test_agentscope_model_uses_only_immutable_writer_and_gateway_port() -> None:
    context = _context()
    writer = FakeInvocationWriter(context)
    gateway = FakeGateway(_success_events())
    model = AgentScopeGatewayChatModel(
        model="gateway-model",
        context=context,
        invocation_writer=writer,
        gateway=gateway,
    )
    message = Msg(name="user", role="user", content=[TextBlock(text="do the task")])

    response = await model([message], temperature=0.2)
    stream = cast(AsyncIterator[ChatResponse], response)
    chunks = [chunk async for chunk in stream]

    assert writer.calls == 1
    assert gateway.calls == 1
    assert writer.messages == [message]
    assert writer.parameters == {"temperature": 0.2}
    assert chunks[-1].is_last is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 10
    assert chunks[-1].usage.cache_creation_input_tokens == 3
    assert chunks[-1].content[0] == TextBlock(
        id="gateway-text:gateway-start", text="hello"
    )
    tool_call = cast(ToolCallBlock, chunks[-1].content[1])
    assert tool_call.name == "search"
    assert tool_call.input == '{"query":"agent"}'


@pytest.mark.asyncio
async def test_gateway_error_is_sanitized() -> None:
    context = _context()
    writer = FakeInvocationWriter(context)
    error_events: list[ModelGatewayStreamEvent] = [
        ResponseErrorEvent(
            **_event_base("gateway-error"),
            event_type="response_error",
            payload=ResponseErrorPayload(
                code="PROVIDER_UNAVAILABLE",
                message="secret provider response body",
                retryable=True,
                submission_state="unknown",
            ),
        )
    ]
    gateway = FakeGateway(error_events)
    model = AgentScopeGatewayChatModel(
        model="gateway-model",
        context=context,
        invocation_writer=writer,
        gateway=gateway,
    )

    response = await model(
        [Msg(name="user", role="user", content=[TextBlock(text="hello")])]
    )
    stream = cast(AsyncIterator[ChatResponse], response)
    with pytest.raises(ModelGatewayBridgeError) as raised:
        _ = [chunk async for chunk in stream]

    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "secret provider response body" not in str(raised.value)
    assert writer.calls == gateway.calls == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_retried_by_agentscope() -> None:
    context = _context()
    writer = FakeInvocationWriter(context)
    gateway = CancellingGateway()
    model = AgentScopeGatewayChatModel(
        model="gateway-model",
        context=context,
        invocation_writer=writer,
        gateway=gateway,
    )

    response = await model._call_api(  # pyright: ignore[reportPrivateUsage]
        "gateway-model",
        [Msg(name="user", role="user", content=[TextBlock(text="hello")])],
    )
    stream = cast(AsyncIterator[ChatResponse], response)
    with pytest.raises(asyncio.CancelledError):
        _ = [chunk async for chunk in stream]

    assert writer.calls == gateway.calls == 1


@pytest.mark.asyncio
async def test_non_primitive_runtime_parameter_fails_before_gateway() -> None:
    context = _context()
    writer = FakeInvocationWriter(context)
    gateway = FakeGateway([])
    model = AgentScopeGatewayChatModel(
        model="gateway-model",
        context=context,
        invocation_writer=writer,
        gateway=gateway,
    )

    with pytest.raises(ModelGatewayBridgeError) as raised:
        await model(
            [Msg(name="user", role="user", content=[TextBlock(text="hello")])],
            unsupported={"nested": True},
        )

    assert raised.value.code == "MODEL_PARAMETERS_INVALID"
    assert writer.calls == gateway.calls == 0
