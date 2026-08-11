"""Versioned payloads for Agent Run orchestration."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunWorkflowStatus = Literal[
    "CREATED",
    "QUEUED",
    "PREPARING",
    "RUNNING",
    "WAITING_APPROVAL",
    "CANCELLING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
]
RunTerminalStatus = Literal["SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"]


class RunRequestedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    run_id: UUID
    initial_execution_attempt: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class AgentRunWorkflowInput(RunRequestedPayloadV1):
    workflow_contract_version: Literal["1.0"] = "1.0"


class RunSpecReference(BaseModel):
    """Immutable RunSpec location returned to Workflow History."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1, max_length=2048)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=10_485_760)


class RunPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    run_id: UUID
    execution_attempt: int = Field(ge=1)
    run_spec: RunSpecReference
    timeout_seconds: int = Field(ge=1, le=86_400)
    runtime_type: Literal["agentscope", "codex"]


class RunSandboxHandle(BaseModel):
    """Small Sandbox/Lease reference safe to persist in Workflow History."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    sandbox_instance_id: str = Field(min_length=3, max_length=255)
    lease_id: str = Field(min_length=3, max_length=255)
    workspace_uri: str = Field(min_length=1, max_length=4096)


class ProvisionRunSandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=1)
    run_spec: RunSpecReference
    runtime_type: Literal["agentscope", "codex"]
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class ReleaseRunSandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=1)
    sandbox: RunSandboxHandle
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class ReleaseRunSandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    sandbox_instance_id: str = Field(min_length=3, max_length=255)
    status: Literal["TERMINATED", "QUARANTINED"]


class ExecuteAgentRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=1)
    run_spec: RunSpecReference
    timeout_seconds: int = Field(ge=1, le=86_400)
    runtime_type: Literal["agentscope", "codex"]
    sandbox: RunSandboxHandle | None = None
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class AssistantTextPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=100_000)


class AssistantArtifactReferencePart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["artifact_reference"] = "artifact_reference"
    artifact_id: str = Field(min_length=1, max_length=255)


class AssistantToolReferencePart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_reference"] = "tool_reference"
    tool_call_id: str = Field(min_length=1, max_length=255)


class AssistantErrorNoticePart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["error_notice"] = "error_notice"
    error_code: str = Field(min_length=1, max_length=64)
    text: str | None = Field(default=None, min_length=1, max_length=2_000)


AssistantContentPart = Annotated[
    AssistantTextPart
    | AssistantArtifactReferencePart
    | AssistantToolReferencePart
    | AssistantErrorNoticePart,
    Field(discriminator="type"),
]


class RuntimeCompletion(BaseModel):
    """Small terminal Runtime summary; high-frequency events stay outside History."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["SUCCEEDED", "FAILED"]
    assistant_content_parts: tuple[AssistantContentPart, ...] = Field(
        default=(), max_length=100
    )
    result_quality: Literal["NORMAL", "SUCCEEDED_WITH_WARNINGS"] | None = None
    runtime_handle_ref: str | None = Field(default=None, max_length=2048)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)
    error_message: str | None = Field(default=None, min_length=1, max_length=500)
    retryable: bool = False

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "RuntimeCompletion":
        if self.status == "SUCCEEDED":
            if not self.assistant_content_parts:
                raise ValueError(
                    "Successful Runtime completion requires assistant content"
                )
            if self.error_code is not None or self.error_message is not None:
                raise ValueError(
                    "Successful Runtime completion cannot contain an error"
                )
        elif self.assistant_content_parts or self.result_quality is not None:
            raise ValueError(
                "Failed Runtime completion cannot contain assistant content"
            )
        elif self.error_code is None or self.error_message is None:
            raise ValueError("Failed Runtime completion requires a safe error")
        return self


class InspectAgentRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class RuntimeInspection(BaseModel):
    """Side-effect free view used before cancellation or Prompt recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    run_id: UUID
    execution_attempt: int = Field(ge=0)
    status: Literal["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "LOST", "UNKNOWN"]
    safe_to_retry: bool = False
    runtime_handle_ref: str | None = Field(default=None, max_length=2048)
    completion: RuntimeCompletion | None = None

    @model_validator(mode="after")
    def validate_inspection_shape(self) -> "RuntimeInspection":
        if self.status in {"SUCCEEDED", "FAILED"}:
            if self.completion is None or self.completion.status != self.status:
                raise ValueError(
                    "Terminal Runtime inspection requires a matching completion"
                )
        elif self.completion is not None:
            raise ValueError("Non-terminal Runtime inspection cannot carry completion")
        if self.safe_to_retry and self.status != "LOST":
            raise ValueError("Only a LOST Runtime can be declared safe to retry")
        return self


class CancelAgentRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class RuntimeCancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    run_id: UUID
    execution_attempt: int = Field(ge=0)
    status: Literal["CANCELLED", "ALREADY_STOPPED", "UNKNOWN"]
    runtime_handle_ref: str | None = Field(default=None, max_length=2048)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


class RecoverAgentRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    tenant_id: UUID
    run_id: UUID
    lost_execution_attempt: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class FinalizeAgentRunCancellationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.1"] = "1.1"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=0)
    cancellation: RuntimeCancellationResult
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class FinalizeAgentRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int = Field(ge=0)
    completion: RuntimeCompletion
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class FinalizeAgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    run_id: UUID
    status: RunTerminalStatus
    assistant_message_id: UUID | None = None


class CancelRunSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str = Field(min_length=1, max_length=128)
    requested_by: UUID
    requested_at: datetime
    reason: str | None = Field(default=None, max_length=500)


class ApprovalDecidedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str = Field(min_length=1, max_length=128)
    approval_id: UUID
    decision_id: UUID
    decision: Literal["APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]
    ticket_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    decided_at: datetime


class AgentRunWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    run_id: UUID
    status: RunWorkflowStatus
    current_activity: str | None = Field(default=None, max_length=128)
    execution_attempt: int = Field(ge=0)
    runtime_handle_ref: str | None = Field(default=None, max_length=2048)
    runtime_session_id: str | None = Field(default=None, max_length=255)
    sandbox_instance_id: str | None = Field(default=None, max_length=255)
    latest_sequence_no: int = Field(ge=0)
    cancel_requested: bool
    waiting_approval_id: str | None = Field(default=None, max_length=255)


class AgentRunWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    run_id: UUID
    status: RunTerminalStatus
    assistant_message_id: UUID | None = None
