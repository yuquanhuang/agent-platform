"""Immutable inputs and outputs for Runtime Bundle compilation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from packages.domain.mcp.model import McpCapabilitySnapshotInput
from packages.domain.resources.model import ResourceContentValue

BundleResourceType = Literal["prompt", "skill", "mcp", "model", "sandbox"]
BundleSourceType = Literal[
    "generated", "prompt", "skill", "mcp", "runtime_config", "static_asset"
]


@dataclass(frozen=True, slots=True)
class BundleResourceInput:
    """One resource version resolved from an immutable Snapshot binding."""

    resource_type: BundleResourceType
    resource_id: UUID
    version_id: UUID
    content_hash: str
    content: ResourceContentValue
    binding_role: str | None = None
    model_binding_snapshot_id: UUID | None = None
    model_binding_snapshot_hash: str | None = None
    mcp_capability_snapshot: McpCapabilitySnapshotInput | None = None


@dataclass(frozen=True, slots=True)
class BundleArtifactInput:
    """Artifact bytes referenced by a published Skill version."""

    artifact_id: str
    content_hash: str
    content: bytes


@dataclass(frozen=True, slots=True)
class BundleModelBindingSnapshotInput:
    """Identity of a frozen Model Gateway binding snapshot."""

    id: UUID
    tenant_id: UUID
    model_config_version_id: UUID
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class BundleAgentInput:
    """A fully resolved immutable Agent Snapshot graph node."""

    tenant_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    snapshot_id: UUID
    snapshot_hash: str
    snapshot_content: dict[str, JsonValue]
    created_at: datetime
    resources: tuple[BundleResourceInput, ...]
    children: tuple[BundleAgentInput, ...] = ()


@dataclass(frozen=True, slots=True)
class BundleFile:
    """A deterministic file in the compiled Bundle."""

    path: str
    content: bytes
    mode: str
    source_type: BundleSourceType
    source_id: str | None


@dataclass(frozen=True, slots=True)
class CompiledRuntimeBundle:
    """Compiler output kept in memory until the Release/Store stage."""

    bundle_id: str
    tenant_id: UUID
    agent_id: UUID
    snapshot_id: UUID
    runtime_type: Literal["agentscope"]
    compiler_name: Literal["AgentScopeBundleCompiler"]
    compiler_version: str
    manifest: dict[str, JsonValue]
    files: tuple[BundleFile, ...]
    content_hash: str
    created_at: datetime

    @property
    def size_bytes(self) -> int:
        return sum(len(file.content) for file in self.files)

    def file(self, path: str) -> BundleFile:
        for file in self.files:
            if file.path == path:
                return file
        raise KeyError(path)
