"""Frozen Model Gateway V1 request, response, usage and stream contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Capability = Literal["stream", "tools", "vision", "structured_output", "reasoning"]
FinishReason = Literal["stop", "length", "tool_call", "cancelled", "error", "unknown"]
SubmissionState = Literal["not_submitted", "submitted", "unknown"]


class ImmutableReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(max_length=2048)
    hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str = Field(pattern=r"^\d+(\.\d{1,8})?$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ModelGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    tenant_id: str
    user_id: str
    agent_id: str
    snapshot_id: str
    run_id: str
    execution_attempt: int | None = Field(default=None, ge=1)
    model_binding_id: str
    prompt_ref: ImmutableReference
    tools_ref: ImmutableReference
    capability_requirements: list[Capability]
    parameters: dict[str, str | float | int | bool | None] = Field(
        default_factory=dict, max_length=50
    )
    stream: bool
    timeout_seconds: int = Field(ge=1, le=600)
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget: Money | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)
    authorization_token: str = Field(min_length=16, max_length=4096, repr=False)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    estimated: bool
    cost: Money | None = None


class ResponseStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextDeltaPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delta: str = Field(min_length=1, max_length=65536)


class ToolCallDeltaPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str | None = Field(default=None, max_length=255)
    arguments_patch: str = Field(max_length=65536)


class UsagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: ModelUsage


class ResponseCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finish_reason: Literal["stop", "length", "tool_call", "cancelled", "unknown"]
    output_ref: ImmutableReference


class ResponseErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64)
    message: str = Field(max_length=4000)
    retryable: bool
    submission_state: SubmissionState


class _StreamEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    provider: str
    model: str
    provider_request_id: str | None
    occurred_at: datetime


class ResponseStartedEvent(_StreamEventBase):
    event_type: Literal["response_started"]
    payload: ResponseStartedPayload


class TextDeltaEvent(_StreamEventBase):
    event_type: Literal["text_delta"]
    payload: TextDeltaPayload


class ToolCallDeltaEvent(_StreamEventBase):
    event_type: Literal["tool_call_delta"]
    payload: ToolCallDeltaPayload


class UsageEvent(_StreamEventBase):
    event_type: Literal["usage"]
    payload: UsagePayload


class ResponseCompletedEvent(_StreamEventBase):
    event_type: Literal["response_completed"]
    payload: ResponseCompletedPayload


class ResponseErrorEvent(_StreamEventBase):
    event_type: Literal["response_error"]
    payload: ResponseErrorPayload


ModelGatewayStreamEvent = Annotated[
    ResponseStartedEvent
    | TextDeltaEvent
    | ToolCallDeltaEvent
    | UsageEvent
    | ResponseCompletedEvent
    | ResponseErrorEvent,
    Field(discriminator="event_type"),
]


class ModelGatewayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    provider_request_id: str | None
    finish_reason: FinishReason
    output_ref: ImmutableReference
    usage: ModelUsage
    fallback_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=100)
