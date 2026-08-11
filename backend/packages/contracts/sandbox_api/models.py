"""Frozen internal Sandbox Manager HTTP request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

SANDBOX_API_SCHEMA_VERSION = "1.0"

Identifier = Annotated[
    str,
    Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
TraceId = Annotated[str, Field(min_length=1, max_length=128)]
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
ImageDigest = Annotated[
    str, Field(pattern=r"^[^@\s]+@sha256:[a-f0-9]{64}$", max_length=2048)
]
ImmutablePolicyRef = Annotated[
    str,
    Field(
        pattern=r"^immutable://sandbox-policy/[A-Za-z0-9._:/-]+$",
        max_length=2048,
    ),
]
BundleRef = Annotated[
    str,
    Field(pattern=r"^bundle://tenant/[A-Za-z0-9._:/-]+$", max_length=2048),
]
WorkspaceUri = Annotated[
    str,
    Field(pattern=r"^workspace://tenant/[A-Za-z0-9._:/-]+$", max_length=4096),
]
CapabilityRef = Annotated[
    str,
    Field(pattern=r"^capability://[A-Za-z0-9._:/-]+$", max_length=2048),
]

SandboxStatus = Literal[
    "REQUESTED",
    "PROVISIONING",
    "READY",
    "IN_USE",
    "FAILED",
    "QUARANTINED",
    "TERMINATING",
    "TERMINATED",
]
ProcessStatus = Literal[
    "STARTING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLING",
    "CANCELLED",
    "TERMINATED",
]


class SandboxApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SandboxProvisionRequest(SandboxApiModel):
    schema_version: Literal["1.0"] = SANDBOX_API_SCHEMA_VERSION
    tenant_id: Identifier
    user_id: Identifier
    session_id: Identifier
    run_id: Identifier
    execution_attempt: int = Field(ge=1)
    scope: Literal["run", "session"]
    policy_ref: ImmutablePolicyRef
    policy_hash: Sha256Digest
    bundle_ref: BundleRef
    bundle_hash: Sha256Digest
    workspace_uri: WorkspaceUri
    provision_token: SecretStr = Field(min_length=16, max_length=4096, repr=False)
    trace_id: TraceId


class SandboxOperationAccepted(SandboxApiModel):
    sandbox_id: Identifier
    operation_id: Identifier
    status: Literal["ACCEPTED"] = "ACCEPTED"
    status_url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_status_url(self) -> SandboxOperationAccepted:
        expected = f"/api/v1/operations/{self.operation_id}"
        if self.status_url != expected:
            raise ValueError("status_url must reference the accepted operation")
        return self


class SandboxDetailResponse(SandboxApiModel):
    sandbox_id: Identifier
    status: SandboxStatus
    scope: Literal["run", "session"]
    image_digest: ImageDigest
    policy_hash: Sha256Digest
    bundle_hash: Sha256Digest
    workspace_uri: WorkspaceUri
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> SandboxDetailResponse:
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class SandboxResourceUsage(SandboxApiModel):
    cpu_seconds: float = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    disk_bytes: int = Field(ge=0)
    pids_current: int = Field(ge=0)
    sampled_at: datetime

    @field_validator("sampled_at")
    @classmethod
    def validate_sampled_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "sampled_at")


class SandboxMainProcess(SandboxApiModel):
    process_id: Identifier
    status: ProcessStatus
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def validate_process_timestamps(self) -> SandboxMainProcess:
        if self.started_at is not None:
            _require_utc(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_utc(self.finished_at, "finished_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot precede started_at")
        if self.exit_code is not None and self.status not in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TERMINATED",
        }:
            raise ValueError("exit_code is only valid for a terminal process")
        return self


class SandboxLeaseRequest(SandboxApiModel):
    schema_version: Literal["1.0"] = SANDBOX_API_SCHEMA_VERSION
    run_id: Identifier
    execution_attempt: int = Field(ge=1)
    execution_fencing_token: SecretStr = Field(
        min_length=16, max_length=4096, repr=False
    )
    ttl_seconds: int = Field(ge=1, le=3600)
    trace_id: TraceId


class SandboxLeaseControlRequest(SandboxApiModel):
    """Fencing proof required for Lease-holder side effects."""

    schema_version: Literal["1.0"] = SANDBOX_API_SCHEMA_VERSION
    run_id: Identifier
    execution_attempt: int = Field(ge=1)
    execution_fencing_token: SecretStr = Field(
        min_length=16, max_length=4096, repr=False
    )
    trace_id: TraceId


class SandboxLeaseResponse(SandboxApiModel):
    sandbox_id: Identifier
    lease_id: Identifier
    run_id: Identifier
    execution_attempt: int = Field(ge=1)
    execution_fencing_token_hash: Sha256Digest
    acquired_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_lease_window(self) -> SandboxLeaseResponse:
        _require_utc(self.acquired_at, "acquired_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must follow acquired_at")
        return self


class SandboxInspectResponse(SandboxApiModel):
    sandbox_id: Identifier
    status: SandboxStatus
    resource_usage: SandboxResourceUsage | None = None
    main_process: SandboxMainProcess | None = None
    lease: SandboxLeaseResponse | None = None
    network_policy_hash: Sha256Digest
    bundle_hash: Sha256Digest
    provider_observed_at: datetime

    @field_validator("provider_observed_at")
    @classmethod
    def validate_provider_observed_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "provider_observed_at")


class SandboxProcessRequest(SandboxLeaseControlRequest):
    process_id: Identifier
    argv: list[str] = Field(min_length=1, max_length=64)
    working_directory: WorkspaceUri
    environment_refs: list[CapabilityRef] = Field(default_factory=list, max_length=50)
    stdin_mode: Literal["PIPE", "CLOSED"] = "PIPE"
    stdout_mode: Literal["PIPE"] = "PIPE"
    timeout_seconds: int = Field(ge=1, le=86400)
    trace_id: TraceId

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        for argument in value:
            if not argument or len(argument) > 4096:
                raise ValueError("argv entries must contain 1 to 4096 characters")
            if "\x00" in argument or any(ord(character) < 32 for character in argument):
                raise ValueError(
                    "argv entries cannot contain NUL or control characters"
                )
        return value


class SandboxProcessControlRequest(SandboxLeaseControlRequest):
    """Fencing proof for cancelling a process owned by the current Lease."""


class SandboxReleaseRequest(SandboxLeaseControlRequest):
    """Fencing proof for releasing the current Run Lease."""


class SandboxProcessResponse(SandboxApiModel):
    sandbox_id: Identifier
    process_id: Identifier
    status: ProcessStatus


class SandboxActionResponse(SandboxApiModel):
    sandbox_id: Identifier
    action: Literal["terminate", "release", "destroy"]
    status: SandboxStatus
    changed: bool


class SandboxProcessActionResponse(SandboxApiModel):
    sandbox_id: Identifier
    process_id: Identifier
    action: Literal["cancel_process"] = "cancel_process"
    status: ProcessStatus
    changed: bool


def _require_utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value
