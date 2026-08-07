"""AgentScope chat model backed exclusively by the platform Model Gateway."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from copy import deepcopy
from typing import Any, Protocol

from agentscope.credential import CredentialBase
from agentscope.message import DataBlock, Msg, TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, FinishedReason
from agentscope.tool import ToolChoice

from packages.contracts.model_gateway import (
    ModelGatewayRequest,
    ModelGatewayStreamEvent,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseStartedEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    UsageEvent,
)
from packages.contracts.public import TenantContext

ModelParameter = str | float | int | bool | None


class AgentScopeInvocationWriter(Protocol):
    """Persist dynamic AgentScope input and return immutable gateway refs."""

    async def create_request(
        self,
        context: TenantContext,
        messages: list[Msg],
        tools: list[dict[str, Any]] | None,
        tool_choice: ToolChoice | None,
        parameters: Mapping[str, ModelParameter],
    ) -> ModelGatewayRequest: ...


class ModelGatewayStreamPort(Protocol):
    """Narrow streaming port implemented by the platform Model Gateway."""

    def stream(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> AsyncIterator[ModelGatewayStreamEvent]: ...


class ModelGatewayBridgeError(RuntimeError):
    """Sanitized bridge failure that never exposes provider error details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        submission_state: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.submission_state = submission_state


class AgentScopeGatewayChatModel(ChatModelBase):
    """AgentScope model adapter that cannot instantiate provider SDK clients."""

    class Parameters(ChatModelBase.Parameters):
        """No provider parameters are owned by AgentScope."""

    def __init__(
        self,
        *,
        model: str,
        context: TenantContext,
        invocation_writer: AgentScopeInvocationWriter,
        gateway: ModelGatewayStreamPort,
        context_size: int = 32768,
    ) -> None:
        super().__init__(
            credential=CredentialBase(name="platform-model-gateway"),
            model=model,
            parameters=self.Parameters(),
            stream=True,
            max_retries=0,
            retry_delay=0.0,
            context_size=context_size,
        )
        self._context = context
        self._invocation_writer = invocation_writer
        self._gateway = gateway

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if model_name != self.model:
            raise ModelGatewayBridgeError(
                "MODEL_NAME_MISMATCH",
                "AgentScope attempted to change the frozen model selection.",
                submission_state="not_submitted",
            )
        parameters = _validate_parameters(kwargs)
        try:
            request = await self._invocation_writer.create_request(
                self._context,
                messages,
                tools,
                tool_choice,
                parameters,
            )
        except asyncio.CancelledError:
            raise
        except ModelGatewayBridgeError:
            raise
        except Exception as error:
            raise ModelGatewayBridgeError(
                "INVOCATION_WRITE_FAILED",
                "The model invocation could not be committed to immutable storage.",
                submission_state="not_submitted",
            ) from error
        if not request.stream:
            raise ModelGatewayBridgeError(
                "GATEWAY_STREAM_REQUIRED",
                "The AgentScope runtime requires a streaming gateway request.",
                submission_state="not_submitted",
            )
        return self._stream_gateway(request)

    async def _stream_gateway(
        self, request: ModelGatewayRequest
    ) -> AsyncGenerator[ChatResponse, None]:
        content: list[TextBlock | ToolCallBlock | ThinkingBlock | DataBlock] = []
        text_block_id: str | None = None
        tool_blocks: dict[str, ToolCallBlock] = {}
        usage: ChatUsage | None = None
        response_id: str | None = None
        completed = False

        async for event in self._gateway.stream(self._context, request):
            if response_id is None:
                response_id = event.event_id
            if isinstance(event, ResponseStartedEvent):
                continue
            if isinstance(event, TextDeltaEvent):
                if text_block_id is None:
                    text_block_id = f"gateway-text:{response_id}"
                    content.append(TextBlock(id=text_block_id, text=""))
                text_block = next(
                    block
                    for block in content
                    if isinstance(block, TextBlock) and block.id == text_block_id
                )
                text_block.text += event.payload.delta
                yield ChatResponse(
                    id=response_id,
                    content=[TextBlock(id=text_block_id, text=event.payload.delta)],
                    is_last=False,
                )
                continue
            if isinstance(event, ToolCallDeltaEvent):
                tool_name = event.payload.tool_name
                current = tool_blocks.get(event.payload.tool_call_id)
                if current is None:
                    if not tool_name:
                        raise ModelGatewayBridgeError(
                            "GATEWAY_TOOL_NAME_MISSING",
                            "The Model Gateway emitted tool arguments before a tool name.",
                        )
                    current = ToolCallBlock(
                        id=event.payload.tool_call_id,
                        name=tool_name,
                        input="",
                    )
                    tool_blocks[current.id] = current
                    content.append(current)
                elif tool_name is not None and tool_name != current.name:
                    raise ModelGatewayBridgeError(
                        "GATEWAY_TOOL_NAME_CHANGED",
                        "The Model Gateway changed a tool name during streaming.",
                    )
                current.input += event.payload.arguments_patch
                yield ChatResponse(
                    id=response_id,
                    content=[
                        ToolCallBlock(
                            id=current.id,
                            name=current.name,
                            input=event.payload.arguments_patch,
                        )
                    ],
                    is_last=False,
                )
                continue
            if isinstance(event, UsageEvent):
                usage = ChatUsage(
                    input_tokens=event.payload.usage.input_tokens,
                    output_tokens=event.payload.usage.output_tokens,
                    time=0.0,
                    cache_creation_input_tokens=(
                        event.payload.usage.cache_write_tokens
                    ),
                    cache_input_tokens=event.payload.usage.cache_read_tokens,
                )
                yield ChatResponse(
                    id=response_id,
                    content=[],
                    is_last=False,
                    usage=usage,
                )
                continue
            if isinstance(event, ResponseErrorEvent):
                raise ModelGatewayBridgeError(
                    event.payload.code,
                    "The Model Gateway request failed.",
                    retryable=event.payload.retryable,
                    submission_state=event.payload.submission_state,
                )
            completed_event: ResponseCompletedEvent = event
            completed = True
            finished_reason = (
                FinishedReason.INTERRUPTED
                if completed_event.payload.finish_reason == "cancelled"
                else FinishedReason.COMPLETED
            )
            yield ChatResponse(
                id=response_id,
                content=deepcopy(content),
                is_last=True,
                usage=usage,
                finished_reason=finished_reason,
            )
            break
        if not completed:
            raise ModelGatewayBridgeError(
                "GATEWAY_STREAM_INCOMPLETE",
                "The Model Gateway stream ended without a completion event.",
            )


def _validate_parameters(values: Mapping[str, Any]) -> dict[str, ModelParameter]:
    if len(values) > 50:
        raise ModelGatewayBridgeError(
            "MODEL_PARAMETERS_INVALID",
            "Too many model parameters were supplied by the runtime.",
            submission_state="not_submitted",
        )
    normalized: dict[str, ModelParameter] = {}
    for key, value in values.items():
        if len(key) > 128:
            raise ModelGatewayBridgeError(
                "MODEL_PARAMETERS_INVALID",
                "A model parameter name is invalid.",
                submission_state="not_submitted",
            )
        if value is not None and not isinstance(value, (str, float, int, bool)):
            raise ModelGatewayBridgeError(
                "MODEL_PARAMETERS_INVALID",
                "A model parameter value is not supported by the gateway contract.",
                submission_state="not_submitted",
            )
        normalized[key] = value
    return normalized
