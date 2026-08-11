"""Application orchestration for immutable Runtime Bundle compilation."""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from pydantic import JsonValue

from packages.application.publishing import SnapshotReader
from packages.contracts.public import TenantContext
from packages.domain.bundles.model import BundleResourceType
from packages.domain.public import (
    BundleAgentInput,
    BundleArtifactInput,
    BundleCompilationError,
    BundleModelBindingSnapshotInput,
    BundleResourceInput,
    CompiledRuntimeBundle,
    McpCapabilitySnapshotInput,
    ResourceVersionRecord,
    compile_agentscope_bundle,
)
from packages.domain.resources.model import ResourceContentSkill, ResourceType

_RESOURCE_TYPE_MAP: dict[str, ResourceType] = {
    "prompt": "prompt",
    "skill": "skill",
    "mcp": "mcp",
    "model": "model_config",
    "sandbox": "sandbox_profile",
}
_MAX_GRAPH_DEPTH = 32


class BundleInputReader(Protocol):
    """Read only immutable Resource Version and model binding facts."""

    async def get_resource_version(
        self,
        context: TenantContext,
        *,
        resource_id: UUID,
        version_id: UUID,
    ) -> ResourceVersionRecord | None: ...

    async def get_model_binding_snapshot(
        self,
        context: TenantContext,
        *,
        snapshot_id: UUID,
    ) -> BundleModelBindingSnapshotInput | None: ...

    async def get_mcp_capability_snapshot(
        self,
        context: TenantContext,
        *,
        published_version_id: UUID,
    ) -> McpCapabilitySnapshotInput | None: ...


class BundleArtifactReader(Protocol):
    """Read immutable Artifact bytes without exposing storage credentials."""

    async def get_artifact(
        self, context: TenantContext, *, artifact_id: str
    ) -> bytes | None: ...


class AgentScopeBundleCompilationService:
    """Load a Snapshot graph and compile it without reading any Draft."""

    def __init__(
        self,
        snapshot_reader: SnapshotReader,
        input_reader: BundleInputReader,
        artifact_reader: BundleArtifactReader,
    ) -> None:
        self._snapshots = snapshot_reader
        self._inputs = input_reader
        self._artifacts = artifact_reader

    async def compile_bundle(
        self, context: TenantContext, *, snapshot_id: UUID
    ) -> CompiledRuntimeBundle:
        tenant_id = _uuid(context.tenant_id, "tenant_id")
        cache: dict[UUID, BundleAgentInput] = {}
        artifacts: dict[str, BundleArtifactInput] = {}
        root = await self._load_node(
            context,
            tenant_id=tenant_id,
            snapshot_id=snapshot_id,
            expected_agent_id=None,
            expected_version_id=None,
            expected_hash=None,
            active=frozenset(),
            cache=cache,
            artifacts=artifacts,
            depth=0,
        )
        return compile_agentscope_bundle(root, artifacts.values())

    async def _load_node(
        self,
        context: TenantContext,
        *,
        tenant_id: UUID,
        snapshot_id: UUID,
        expected_agent_id: UUID | None,
        expected_version_id: UUID | None,
        expected_hash: str | None,
        active: frozenset[UUID],
        cache: dict[UUID, BundleAgentInput],
        artifacts: dict[str, BundleArtifactInput],
        depth: int,
    ) -> BundleAgentInput:
        if depth > _MAX_GRAPH_DEPTH:
            raise BundleCompilationError(
                "BUNDLE_GRAPH_TOO_DEEP", "Agent graph exceeds the compiler limit."
            )
        if snapshot_id in active:
            raise BundleCompilationError(
                "AGENT_GRAPH_RECURSIVE", "Agent Snapshot graph is recursive."
            )
        cached = cache.get(snapshot_id)
        if cached is not None:
            _validate_expected_node(
                cached,
                expected_agent_id=expected_agent_id,
                expected_version_id=expected_version_id,
                expected_hash=expected_hash,
            )
            return cached
        snapshot = await self._snapshots.get_snapshot(context, snapshot_id=snapshot_id)
        if snapshot is None:
            raise BundleCompilationError(
                "SNAPSHOT_NOT_FOUND", "An immutable Agent Snapshot is unavailable."
            )
        version = await self._snapshots.get_version(
            context, agent_version_id=snapshot.agent_version_id
        )
        if version is None:
            raise BundleCompilationError(
                "AGENT_VERSION_NOT_FOUND", "The Snapshot Agent Version is unavailable."
            )
        if snapshot.tenant_id != tenant_id or version.tenant_id != tenant_id:
            raise BundleCompilationError(
                "TENANT_ISOLATION", "Cross-tenant Bundle inputs are not allowed."
            )
        bindings = _snapshot_bindings(snapshot.content)
        resources: list[BundleResourceInput] = []
        children: list[BundleAgentInput] = []
        next_active = active | {snapshot_id}
        for binding in bindings:
            binding_type = _binding_type(binding)
            resource_id = _binding_uuid(binding, "resource_id")
            version_id = _binding_uuid(binding, "version_id")
            content_hash = _binding_text(binding, "content_hash")
            if binding_type == "agent":
                child_snapshot_id = _binding_uuid(binding, "agent_snapshot_id")
                children.append(
                    await self._load_node(
                        context,
                        tenant_id=tenant_id,
                        snapshot_id=child_snapshot_id,
                        expected_agent_id=resource_id,
                        expected_version_id=version_id,
                        expected_hash=content_hash,
                        active=next_active,
                        cache=cache,
                        artifacts=artifacts,
                        depth=depth + 1,
                    )
                )
                continue
            registry_type = _RESOURCE_TYPE_MAP.get(binding_type)
            if registry_type is None:
                raise BundleCompilationError(
                    "UNSUPPORTED_BUNDLE_RESOURCE",
                    f"Resource type {binding_type!r} is not supported by this compiler.",
                )
            record = await self._inputs.get_resource_version(
                context, resource_id=resource_id, version_id=version_id
            )
            if record is None:
                raise BundleCompilationError(
                    "RESOURCE_VERSION_NOT_FOUND",
                    "A Snapshot resource version is unavailable.",
                )
            if (
                record.tenant_id != tenant_id
                or record.definition_id != resource_id
                or record.id != version_id
                or record.content_hash != content_hash
                or record.content.resource_type != registry_type
            ):
                raise BundleCompilationError(
                    "SNAPSHOT_INPUT_MISMATCH",
                    "A Resource Version does not match its immutable Snapshot binding.",
                )
            model_snapshot_id: UUID | None = None
            model_snapshot_hash: str | None = None
            mcp_snapshot: McpCapabilitySnapshotInput | None = None
            if binding_type == "model":
                model_snapshot = binding.get("model_binding_snapshot")
                if not isinstance(model_snapshot, dict):
                    raise BundleCompilationError(
                        "MODEL_BINDING_SNAPSHOT_REQUIRED",
                        "A Model route requires an immutable binding snapshot.",
                    )
                model_snapshot_id = _binding_uuid(model_snapshot, "id")
                model_snapshot_hash = _binding_text(model_snapshot, "content_hash")
                frozen_model = await self._inputs.get_model_binding_snapshot(
                    context, snapshot_id=model_snapshot_id
                )
                if (
                    frozen_model is None
                    or frozen_model.tenant_id != tenant_id
                    or frozen_model.model_config_version_id != version_id
                    or frozen_model.snapshot_hash != model_snapshot_hash
                ):
                    raise BundleCompilationError(
                        "MODEL_BINDING_SNAPSHOT_MISMATCH",
                        "The Model binding snapshot does not match the Agent Snapshot.",
                    )
            if binding_type == "mcp":
                mcp_snapshot = await self._inputs.get_mcp_capability_snapshot(
                    context, published_version_id=version_id
                )
                if (
                    mcp_snapshot is None
                    or mcp_snapshot.tenant_id != tenant_id
                    or mcp_snapshot.definition_id != resource_id
                    or mcp_snapshot.published_version_id != version_id
                    or mcp_snapshot.content_hash != content_hash
                ):
                    raise BundleCompilationError(
                        "MCP_CAPABILITY_SNAPSHOT_REQUIRED",
                        "An MCP resource requires passed immutable capability evidence.",
                    )
            resources.append(
                BundleResourceInput(
                    resource_type=cast(BundleResourceType, binding_type),
                    resource_id=resource_id,
                    version_id=version_id,
                    content_hash=content_hash,
                    content=record.content,
                    binding_role=(
                        str(binding["binding_role"])
                        if binding.get("binding_role") is not None
                        else None
                    ),
                    model_binding_snapshot_id=model_snapshot_id,
                    model_binding_snapshot_hash=model_snapshot_hash,
                    mcp_capability_snapshot=mcp_snapshot,
                )
            )
            if isinstance(record.content, ResourceContentSkill):
                for skill_file in record.content.files:
                    existing = artifacts.get(skill_file.artifact_id)
                    if existing is not None:
                        if existing.content_hash != skill_file.content_hash:
                            raise BundleCompilationError(
                                "BUNDLE_ARTIFACT_HASH_MISMATCH",
                                "One Artifact ID is referenced with multiple hashes.",
                            )
                        continue
                    content = await self._artifacts.get_artifact(
                        context, artifact_id=skill_file.artifact_id
                    )
                    if content is None:
                        raise BundleCompilationError(
                            "BUNDLE_ARTIFACT_NOT_FOUND",
                            "A published Skill Artifact is unavailable.",
                        )
                    artifacts[skill_file.artifact_id] = BundleArtifactInput(
                        artifact_id=skill_file.artifact_id,
                        content_hash=skill_file.content_hash,
                        content=content,
                    )
        node = BundleAgentInput(
            tenant_id=tenant_id,
            agent_id=version.agent_id,
            agent_version_id=version.id,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.content_hash,
            snapshot_content=snapshot.content,
            created_at=snapshot.created_at,
            resources=tuple(resources),
            children=tuple(sorted(children, key=lambda child: str(child.agent_id))),
        )
        _validate_expected_node(
            node,
            expected_agent_id=expected_agent_id,
            expected_version_id=expected_version_id,
            expected_hash=expected_hash,
        )
        cache[snapshot_id] = node
        return node


def _snapshot_bindings(content: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    value = content.get("bindings")
    if not isinstance(value, list):
        raise BundleCompilationError(
            "SNAPSHOT_SCHEMA_INVALID", "Snapshot bindings must be an array."
        )
    result: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BundleCompilationError(
                "SNAPSHOT_SCHEMA_INVALID", "Snapshot binding must be an object."
            )
        result.append(item)
    return result


def _binding_type(binding: dict[str, JsonValue]) -> str:
    value = binding.get("resource_type")
    if not isinstance(value, str):
        raise BundleCompilationError(
            "SNAPSHOT_SCHEMA_INVALID", "Snapshot binding type is invalid."
        )
    return value


def _binding_uuid(binding: dict[str, JsonValue], field: str) -> UUID:
    value = _binding_text(binding, field)
    return _uuid(value, field)


def _binding_text(binding: dict[str, JsonValue], field: str) -> str:
    value = binding.get(field)
    if not isinstance(value, str) or not value:
        raise BundleCompilationError(
            "SNAPSHOT_SCHEMA_INVALID", f"Snapshot binding {field} is invalid."
        )
    return value


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise BundleCompilationError(
            "SNAPSHOT_SCHEMA_INVALID", f"{field} must be a UUID."
        ) from exc


def _validate_expected_node(
    node: BundleAgentInput,
    *,
    expected_agent_id: UUID | None,
    expected_version_id: UUID | None,
    expected_hash: str | None,
) -> None:
    if (
        (expected_agent_id is not None and node.agent_id != expected_agent_id)
        or (
            expected_version_id is not None
            and node.agent_version_id != expected_version_id
        )
        or (expected_hash is not None and node.snapshot_hash != expected_hash)
    ):
        raise BundleCompilationError(
            "SNAPSHOT_INPUT_MISMATCH",
            "A child Agent does not match its immutable parent Snapshot binding.",
        )
