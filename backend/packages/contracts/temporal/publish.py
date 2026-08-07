"""Versioned payloads for the Agent publication workflow."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReleaseRequestedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    release_id: UUID
    operation_id: UUID
    agent_id: UUID
    release_kind: Literal["PUBLISH", "ROLLBACK"] = "PUBLISH"
    expected_agent_version: int | None = Field(default=None, ge=1)
    requested_snapshot_id: UUID | None = None
    runtime_targets: list[str] = Field(min_length=1, max_length=32)
    run_smoke_test: bool
    activate_on_success: bool
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_release_source(self) -> "ReleaseRequestedPayloadV1":
        if self.release_kind == "PUBLISH":
            if (
                self.expected_agent_version is None
                or self.requested_snapshot_id is not None
            ):
                raise ValueError("Publish Release source is invalid")
        elif (
            self.expected_agent_version is not None
            or self.requested_snapshot_id is None
        ):
            raise ValueError("Rollback Release source is invalid")
        return self


class PublishAgentWorkflowInput(ReleaseRequestedPayloadV1):
    workflow_contract_version: Literal["1.0"] = "1.0"


class PublishAgentWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    release_id: UUID
    status: Literal["SUCCEEDED"] = "SUCCEEDED"


class PublishReleaseFailureInput(PublishAgentWorkflowInput):
    error_code: str = Field(min_length=1, max_length=128)
    error_message: str = Field(min_length=1, max_length=500)
