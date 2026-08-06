"""Versioned payloads for the minimal Temporal connectivity workflow."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TemporalWorkerKind(StrEnum):
    CONTROL = "control"
    RUN = "run"


class WorkflowProbeInput(BaseModel):
    """Small deterministic input used to verify a worker deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    probe_id: UUID
    worker_kind: TemporalWorkerKind
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class WorkflowProbeResult(BaseModel):
    """Durable result emitted by the probe Activity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_contract_version: Literal["1.0"] = "1.0"
    status: Literal["SUCCEEDED"] = "SUCCEEDED"
    workflow_id: str = Field(min_length=1, max_length=255)
    worker_kind: TemporalWorkerKind


class ProbeRequestedPayloadV1(BaseModel):
    """Outbox payload mapped to the probe workflow starter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    probe_id: UUID
    worker_kind: TemporalWorkerKind
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
