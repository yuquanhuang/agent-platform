"""Immutable Agent Version and Snapshot compilation records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from packages.domain.agents import AgentRuntimeType, AgentVisibility, BindingRole

SnapshotBindingType = Literal["prompt", "skill", "mcp", "model", "sandbox", "agent"]
SnapshotDiffCategory = Literal[
    "resource_version",
    "permission",
    "network",
    "sandbox",
    "model",
    "secret_reference",
    "runtime",
]
SnapshotChangeType = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class ResolvedSnapshotBinding:
    """One Draft binding resolved to an immutable resource or Agent version."""

    resource_type: SnapshotBindingType
    resource_id: UUID
    version_id: UUID
    version_no: int
    schema_version: str
    content_hash: str
    binding_role: BindingRole | None = None
    configuration_schema_version: str | None = None
    configuration: dict[str, JsonValue] | None = None
    model_binding_snapshot_id: UUID | None = None
    model_binding_snapshot_hash: str | None = None
    agent_snapshot_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SnapshotCompilationInput:
    """Stable inputs captured from one consistent publication transaction."""

    agent_id: UUID
    code: str
    name: str
    description: str | None
    runtime_type: AgentRuntimeType
    visibility: AgentVisibility
    tags: tuple[str, ...]
    default_language: str
    draft_resource_version: int
    bindings: tuple[ResolvedSnapshotBinding, ...]


@dataclass(frozen=True, slots=True)
class CompiledAgentSnapshot:
    schema_version: str
    content: dict[str, JsonValue]
    content_hash: str
    compiler_input_hash: str


@dataclass(frozen=True, slots=True)
class AgentVersionRecord:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    version_no: int
    created_from_version_id: UUID | None
    release_note: str | None
    created_at: datetime
    created_by: UUID


@dataclass(frozen=True, slots=True)
class AgentSnapshotRecord:
    id: UUID
    tenant_id: UUID
    agent_version_id: UUID
    schema_version: str
    content: dict[str, JsonValue]
    content_hash: str
    compiler_input_hash: str
    created_at: datetime
    created_by: UUID


@dataclass(frozen=True, slots=True)
class SnapshotPublicationRecord:
    version: AgentVersionRecord
    snapshot: AgentSnapshotRecord
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AgentVersionSnapshotRecord:
    version: AgentVersionRecord
    snapshot: AgentSnapshotRecord


@dataclass(frozen=True, slots=True)
class SnapshotChangeRecord:
    category: SnapshotDiffCategory
    path: str
    change_type: SnapshotChangeType
    before: JsonValue | None = None
    after: JsonValue | None = None
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedPreviewBinding:
    resource_type: SnapshotBindingType
    resource_id: UUID
    version_id: UUID
    version_no: int
    content_hash: str
    binding_role: BindingRole | None = None


@dataclass(frozen=True, slots=True)
class PublishPreviewTargetRecord:
    runtime_target_id: str
    current_deployment_id: UUID | None
    current_snapshot_id: UUID | None
    changes: tuple[SnapshotChangeRecord, ...]


@dataclass(frozen=True, slots=True)
class PublishPreviewRecord:
    agent_id: UUID
    expected_agent_version: int
    preview_snapshot_hash: str
    resolved_bindings: tuple[ResolvedPreviewBinding, ...]
    targets: tuple[PublishPreviewTargetRecord, ...]
    ready_to_publish: bool
