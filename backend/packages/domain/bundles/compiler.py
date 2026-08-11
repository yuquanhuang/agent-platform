"""Deterministic AgentScope Bundle compiler and verifier.

This module intentionally has no filesystem, database, Secret Broker, Registry or
AgentScope dependency.  It consumes only immutable Snapshot/resource inputs and
produces bytes that can be persisted by the later Release Workflow.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from datetime import UTC
from typing import Literal, cast
from uuid import UUID

from pydantic import JsonValue

from packages.domain.bundles.model import (
    BundleAgentInput,
    BundleArtifactInput,
    BundleFile,
    BundleResourceInput,
    CompiledRuntimeBundle,
)
from packages.domain.policy import (
    McpPolicySource,
    PolicyCompilationError,
    PolicyResourceSource,
    SkillPolicySource,
    compile_effective_policy,
)
from packages.domain.resources.model import (
    ResourceContentMcp,
    ResourceContentModelConfig,
    ResourceContentPrompt,
    ResourceContentSandboxProfile,
    ResourceContentSkill,
    canonical_content_hash,
    resource_content_json,
)

BUNDLE_MANIFEST_SCHEMA_VERSION = "1.0"
BUNDLE_COMPILER_NAME = "AgentScopeBundleCompiler"
BUNDLE_COMPILER_VERSION = "1.2.0"
BUNDLE_RUNTIME_TYPE = "agentscope"
_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_MODE_RE = re.compile(r"^0[0-7]{3}$")
_MAX_GRAPH_NODES = 500


class BundleCompilationError(ValueError):
    """Stable, fail-closed compiler error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def compile_agentscope_bundle(
    root: BundleAgentInput,
    artifacts: Iterable[BundleArtifactInput] = (),
) -> CompiledRuntimeBundle:
    """Compile one immutable Snapshot graph into a reproducible Bundle."""

    nodes = _flatten_nodes(root)
    artifact_map = _artifact_map(artifacts)
    _validate_nodes(nodes)
    if not any(resource.resource_type == "sandbox" for resource in root.resources):
        raise BundleCompilationError(
            "SANDBOX_POLICY_REQUIRED",
            "The root Agent requires an immutable sandbox profile.",
        )

    files: list[BundleFile] = []
    security_permissions: list[dict[str, object]] = []
    sandbox_policies: list[
        tuple[PolicyResourceSource, ResourceContentSandboxProfile]
    ] = []
    skill_policies: list[SkillPolicySource] = []
    mcp_policies: list[McpPolicySource] = []
    binding_payloads: list[dict[str, object]] = []
    runtime_agents: list[dict[str, object]] = []

    for node in nodes:
        prefix = "" if node is root else f"agents/{node.agent_id}/"
        prompt_paths: list[str] = []
        skill_paths: list[str] = []
        mcp_paths: list[str] = []
        sandbox_path = "security/sandbox-policy.json"
        child_ids = [str(child.agent_id) for child in node.children]

        prompt_resources = [
            resource
            for resource in node.resources
            if resource.resource_type == "prompt"
        ]
        for index, resource in enumerate(
            sorted(prompt_resources, key=_resource_sort_key)
        ):
            content = cast(ResourceContentPrompt, resource.content)
            path = (
                f"{prefix}prompt/system.md"
                if len(prompt_resources) == 1
                else f"{prefix}prompt/{resource.resource_id}.md"
            )
            if index and path in {item.path for item in files}:
                raise BundleCompilationError("DUPLICATE_FILE_PATH", path)
            files.append(
                BundleFile(
                    path=path,
                    content=content.template.encode("utf-8"),
                    mode="0644",
                    source_type="prompt",
                    source_id=str(resource.version_id),
                )
            )
            prompt_paths.append(path)

        for resource in sorted(
            (item for item in node.resources if item.resource_type == "skill"),
            key=_resource_sort_key,
        ):
            content = cast(ResourceContentSkill, resource.content)
            skill_name = content.manifest.metadata.name
            base = f"skills/{skill_name}"
            skill_paths.append(base)
            security_permissions.append(
                {
                    "kind": "skill",
                    "resource_id": str(resource.resource_id),
                    "version_id": str(resource.version_id),
                    "permissions": content.manifest.permissions.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                }
            )
            skill_policies.append(
                SkillPolicySource(
                    source=_policy_source(resource, "skill"),
                    manifest=content.manifest,
                )
            )
            for skill_file in sorted(content.files, key=lambda item: item.path):
                normalized = _safe_relative_path(skill_file.path)
                artifact = artifact_map.get(skill_file.artifact_id)
                if artifact is None:
                    raise BundleCompilationError(
                        "BUNDLE_ARTIFACT_NOT_FOUND", skill_file.artifact_id
                    )
                _verify_hash(
                    artifact.content,
                    skill_file.content_hash,
                    f"skill artifact {skill_file.artifact_id}",
                )
                path = f"{base}/{normalized}"
                files.append(
                    BundleFile(
                        path=path,
                        content=artifact.content,
                        mode="0644",
                        source_type="skill",
                        source_id=str(resource.version_id),
                    )
                )

        for resource in sorted(
            (item for item in node.resources if item.resource_type == "mcp"),
            key=_resource_sort_key,
        ):
            content = cast(ResourceContentMcp, resource.content)
            capability = resource.mcp_capability_snapshot
            mcp_payload = resource_content_json(content)
            permission: dict[str, object] = {
                "kind": "mcp",
                "resource_id": str(resource.resource_id),
                "version_id": str(resource.version_id),
                "allowed_tools": list(content.allowed_tools or []),
            }
            if capability is not None:
                authorized = [
                    tool
                    for tool in capability.tools
                    if tool.name in capability.allowed_tools
                ]
                mcp_payload["capability_evidence"] = {
                    "discovery_id": str(capability.id),
                    "capability_hash": capability.capability_hash,
                    "protocol_version": capability.protocol_version,
                    "server": {
                        "name": capability.server_name,
                        "version": capability.server_version,
                    },
                    "allowed_tools": list(capability.allowed_tools),
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                            "output_schema": tool.output_schema,
                            "schema_hash": tool.schema_hash,
                            "risk_level": tool.risk_level,
                        }
                        for tool in authorized
                    ],
                }
                permission["capability_hash"] = capability.capability_hash
                permission["tools"] = [
                    {
                        "name": tool.name,
                        "schema_hash": tool.schema_hash,
                        "risk_level": tool.risk_level,
                    }
                    for tool in authorized
                ]
                mcp_policies.append(
                    McpPolicySource(
                        source=_policy_source(resource, "mcp"),
                        tools=tuple(authorized),
                    )
                )
            path = f"{prefix}mcp/{resource.resource_id}.json"
            files.append(
                BundleFile(
                    path=path,
                    content=_json_bytes(mcp_payload),
                    mode="0644",
                    source_type="mcp",
                    source_id=str(resource.version_id),
                )
            )
            mcp_paths.append(path)
            security_permissions.append(permission)

        for resource in node.resources:
            if resource.resource_type == "sandbox":
                content = cast(ResourceContentSandboxProfile, resource.content)
                sandbox_policies.append((_policy_source(resource, "sandbox"), content))

        for resource in node.resources:
            binding_payloads.append(
                {
                    "resource_type": _manifest_resource_type(resource.resource_type),
                    "resource_id": str(resource.resource_id),
                    "version_id": str(resource.version_id),
                    "content_hash": resource.content_hash,
                }
            )

        runtime_agents.append(
            {
                "agent_id": str(node.agent_id),
                "agent_version_id": str(node.agent_version_id),
                "snapshot_id": str(node.snapshot_id),
                "snapshot_hash": node.snapshot_hash,
                "code": _agent_field(node, "code"),
                "prompt_paths": prompt_paths,
                "skill_paths": sorted(set(skill_paths)),
                "mcp_paths": mcp_paths,
                "model_routing": node.snapshot_content.get("model_routing"),
                "sandbox_policy_path": sandbox_path,
                "child_agent_ids": child_ids,
                "default_language": _agent_field(node, "default_language"),
            }
        )

    try:
        effective_policy = compile_effective_policy(
            sandbox_policies=(
                (source, content.policy) for source, content in sandbox_policies
            ),
            skills=skill_policies,
            mcps=mcp_policies,
        )
    except PolicyCompilationError as error:
        raise BundleCompilationError(error.code, str(error)) from error
    if effective_policy.decision == "DENY":
        raise BundleCompilationError(
            effective_policy.reason_codes[0],
            "The effective policy denies this Agent graph.",
        )
    effective_policy_payload = cast(
        dict[str, object], json.loads(effective_policy.canonical_json)
    )
    sandbox_bytes = _json_bytes(
        cast(
            dict[str, JsonValue],
            effective_policy.sandbox_policy.model_dump(mode="json"),
        )
    )
    permission_bytes = _json_bytes(
        {
            "schema_version": "bundle-permission-policy/v2",
            "effective_policy_hash": effective_policy.policy_hash,
            "effective_policy": effective_policy_payload,
            "entries": sorted(
                security_permissions,
                key=lambda value: (
                    str(value["kind"]),
                    str(value["resource_id"]),
                    str(value["version_id"]),
                ),
            ),
        }
    )
    files.extend(
        [
            BundleFile(
                path="security/permissions.json",
                content=permission_bytes,
                mode="0644",
                source_type="generated",
                source_id=None,
            ),
            BundleFile(
                path="security/sandbox-policy.json",
                content=sandbox_bytes,
                mode="0644",
                source_type="generated",
                source_id=None,
            ),
        ]
    )
    runtime_bytes = _json_bytes(
        {
            "schema_version": "agentscope-runtime/v1",
            "runtime_type": BUNDLE_RUNTIME_TYPE,
            "root_agent_id": str(root.agent_id),
            "root_snapshot_id": str(root.snapshot_id),
            "agents": runtime_agents,
        }
    )
    files.insert(
        0,
        BundleFile(
            path="runtime.yaml",
            content=runtime_bytes,
            mode="0644",
            source_type="runtime_config",
            source_id=None,
        ),
    )
    files = _deduplicate_files(files)
    if len(files) > 9999:
        raise BundleCompilationError(
            "BUNDLE_FILE_LIMIT_EXCEEDED", "Bundle contains too many payload files."
        )
    entries = tuple(
        _file_entry(file) for file in sorted(files, key=lambda item: item.path)
    )
    file_manifest_bytes = _json_bytes({"schema_version": "1.0", "files": list(entries)})
    files.append(
        BundleFile(
            path="file-manifest.json",
            content=file_manifest_bytes,
            mode="0644",
            source_type="generated",
            source_id=None,
        )
    )
    entries = tuple(
        _file_entry(file) for file in sorted(files, key=lambda item: item.path)
    )
    secret_refs = sorted(_secret_refs(nodes))
    if len(secret_refs) > 100:
        raise BundleCompilationError(
            "BUNDLE_SECRET_LIMIT_EXCEEDED",
            "Bundle contains too many Secret references.",
        )
    bindings = sorted(
        _unique_payloads(binding_payloads),
        key=lambda value: (
            str(value["resource_type"]),
            str(value["resource_id"]),
            str(value["version_id"]),
        ),
    )
    if len(bindings) > 500:
        raise BundleCompilationError(
            "BUNDLE_BINDING_LIMIT_EXCEEDED", "Bundle contains too many bindings."
        )
    security = {
        "permission_policy_hash": _hash_bytes(permission_bytes),
        "sandbox_policy_hash": _hash_bytes(sandbox_bytes),
        "secret_refs": secret_refs,
    }
    manifest_without_hash: dict[str, object] = {
        "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
        "bundle_id": "pending",
        "tenant_id": str(root.tenant_id),
        "agent_id": str(root.agent_id),
        "snapshot_id": str(root.snapshot_id),
        "runtime_type": BUNDLE_RUNTIME_TYPE,
        "compiler": {"name": BUNDLE_COMPILER_NAME, "version": BUNDLE_COMPILER_VERSION},
        "files": list(entries),
        "bindings": bindings,
        "security": security,
    }
    content_hash = _hash_json(manifest_without_hash)
    bundle_id = f"bundle_{content_hash.removeprefix('sha256:')}"
    if root.created_at.tzinfo is None:
        raise BundleCompilationError(
            "SNAPSHOT_TIMESTAMP_INVALID", "Snapshot created_at must include a timezone."
        )
    created_at = root.created_at.astimezone(UTC)
    manifest_object: dict[str, object] = {
        **manifest_without_hash,
        "bundle_id": bundle_id,
        "content_hash": content_hash,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }
    manifest = cast(dict[str, JsonValue], manifest_object)
    manifest_bytes = _json_bytes(manifest_object)
    files.append(
        BundleFile(
            path="manifest.json",
            content=manifest_bytes,
            mode="0644",
            source_type="generated",
            source_id=None,
        )
    )
    bundle = CompiledRuntimeBundle(
        bundle_id=bundle_id,
        tenant_id=root.tenant_id,
        agent_id=root.agent_id,
        snapshot_id=root.snapshot_id,
        runtime_type="agentscope",
        compiler_name="AgentScopeBundleCompiler",
        compiler_version=BUNDLE_COMPILER_VERSION,
        manifest=manifest,
        files=tuple(sorted(files, key=lambda item: item.path)),
        content_hash=content_hash,
        created_at=created_at,
    )
    verify_compiled_bundle(bundle)
    return bundle


def verify_compiled_bundle(bundle: CompiledRuntimeBundle) -> None:
    """Verify internal file, hash, ordering and manifest invariants."""

    manifest = cast(dict[str, object], bundle.manifest)
    required = {
        "schema_version",
        "bundle_id",
        "tenant_id",
        "agent_id",
        "snapshot_id",
        "runtime_type",
        "compiler",
        "files",
        "bindings",
        "security",
        "content_hash",
        "created_at",
    }
    if set(manifest) - {
        *required,
        "signature_ref",
        "sbom_ref",
    } or not required <= set(manifest):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest fields invalid"
        )
    if manifest["runtime_type"] != "agentscope":
        raise BundleCompilationError("MANIFEST_SCHEMA_INVALID", "Unsupported runtime")
    if (
        manifest["schema_version"] != BUNDLE_MANIFEST_SCHEMA_VERSION
        or manifest["bundle_id"] != bundle.bundle_id
        or manifest["tenant_id"] != str(bundle.tenant_id)
        or manifest["agent_id"] != str(bundle.agent_id)
        or manifest["snapshot_id"] != str(bundle.snapshot_id)
        or manifest["content_hash"] != bundle.content_hash
    ):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest identity does not match Bundle"
        )
    compiler = manifest["compiler"]
    if not isinstance(compiler, dict) or compiler != {
        "name": BUNDLE_COMPILER_NAME,
        "version": BUNDLE_COMPILER_VERSION,
    }:
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest compiler identity is invalid"
        )
    file_entries_value = manifest["files"]
    file_entries: list[dict[str, object]] = []
    if isinstance(file_entries_value, list):
        for raw_entry in cast(list[object], file_entries_value):
            if not isinstance(raw_entry, dict):
                raise BundleCompilationError(
                    "MANIFEST_FILES_INVALID", "File entry must be an object"
                )
            file_entries.append(cast(dict[str, object], raw_entry))
    if not file_entries:
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest files invalid"
        )
    paths = [str(entry["path"]) for entry in file_entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise BundleCompilationError(
            "MANIFEST_FILES_INVALID", "File paths must be unique and sorted"
        )
    files_by_path = {file.path: file for file in bundle.files}
    if set(files_by_path) != {*paths, "manifest.json"}:
        raise BundleCompilationError(
            "MANIFEST_FILES_INVALID", "Bundle files do not match manifest"
        )
    for entry in file_entries:
        path = str(entry.get("path"))
        file = files_by_path.get(path)
        if file is None:
            raise BundleCompilationError("MANIFEST_FILES_INVALID", path)
        if entry.get("hash") != _hash_bytes(file.content):
            raise BundleCompilationError("BUNDLE_FILE_HASH_MISMATCH", path)
        if (
            entry.get("size_bytes") != len(file.content)
            or entry.get("mode") != file.mode
        ):
            raise BundleCompilationError("MANIFEST_FILES_INVALID", path)
        if len(file.content) > 1073741824:
            raise BundleCompilationError("MANIFEST_FILES_INVALID", path)
        _safe_relative_path(path)
        if not _HASH_RE.fullmatch(str(entry.get("hash"))):
            raise BundleCompilationError("MANIFEST_SCHEMA_INVALID", path)
        if not _MODE_RE.fullmatch(str(entry.get("mode"))):
            raise BundleCompilationError("MANIFEST_SCHEMA_INVALID", path)
        if entry.get("source_type") not in {
            "generated",
            "prompt",
            "skill",
            "mcp",
            "runtime_config",
            "static_asset",
        }:
            raise BundleCompilationError("MANIFEST_SCHEMA_INVALID", path)
    manifest_file = files_by_path.get("manifest.json")
    if manifest_file is None or json.loads(manifest_file.content) != manifest:
        raise BundleCompilationError("MANIFEST_FILE_MISMATCH", "manifest.json mismatch")
    file_manifest = files_by_path.get("file-manifest.json")
    expected_file_manifest_entries = [
        entry for entry in file_entries if entry.get("path") != "file-manifest.json"
    ]
    parsed_file_manifest: object = (
        json.loads(file_manifest.content) if file_manifest is not None else None
    )
    parsed_file_manifest_files = (
        cast(dict[str, object], parsed_file_manifest).get("files")
        if isinstance(parsed_file_manifest, dict)
        else None
    )
    if parsed_file_manifest_files != expected_file_manifest_entries:
        raise BundleCompilationError(
            "FILE_MANIFEST_MISMATCH", "file-manifest.json mismatch"
        )
    semantic = dict(manifest)
    semantic.pop("content_hash", None)
    semantic.pop("created_at", None)
    semantic.pop("signature_ref", None)
    semantic.pop("sbom_ref", None)
    semantic["bundle_id"] = "pending"
    if _hash_json(semantic) != manifest["content_hash"]:
        raise BundleCompilationError(
            "BUNDLE_CONTENT_HASH_MISMATCH", "content_hash mismatch"
        )
    if bundle.content_hash != manifest["content_hash"]:
        raise BundleCompilationError(
            "BUNDLE_CONTENT_HASH_MISMATCH", "bundle content_hash mismatch"
        )
    if bundle.bundle_id != f"bundle_{bundle.content_hash.removeprefix('sha256:')}":
        raise BundleCompilationError(
            "BUNDLE_CONTENT_HASH_MISMATCH", "bundle_id is not content addressed"
        )
    _verify_manifest_bindings(manifest.get("bindings"))
    _verify_manifest_security(manifest.get("security"), files_by_path)


def _verify_manifest_bindings(value: object) -> None:
    if not isinstance(value, list):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest bindings are invalid"
        )
    typed_bindings = cast(list[object], value)
    if len(typed_bindings) > 500:
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest bindings are invalid"
        )
    identities: list[tuple[str, str, str]] = []
    for raw_binding in typed_bindings:
        if not isinstance(raw_binding, dict):
            raise BundleCompilationError(
                "MANIFEST_SCHEMA_INVALID", "Manifest binding must be an object"
            )
        binding = cast(dict[str, object], raw_binding)
        if set(binding) != {
            "resource_type",
            "resource_id",
            "version_id",
            "content_hash",
        } or binding.get("resource_type") not in {
            "prompt",
            "skill",
            "mcp",
            "model",
            "knowledge",
            "sandbox",
            "agent",
        }:
            raise BundleCompilationError(
                "MANIFEST_SCHEMA_INVALID", "Manifest binding fields are invalid"
            )
        if not _HASH_RE.fullmatch(str(binding.get("content_hash"))):
            raise BundleCompilationError(
                "MANIFEST_SCHEMA_INVALID", "Manifest binding hash is invalid"
            )
        identities.append(
            (
                str(binding["resource_type"]),
                str(binding["resource_id"]),
                str(binding["version_id"]),
            )
        )
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest bindings must be unique and sorted"
        )


def _verify_manifest_security(
    value: object, files_by_path: Mapping[str, BundleFile]
) -> None:
    if not isinstance(value, dict):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest security is invalid"
        )
    security = cast(dict[str, object], value)
    if set(security) != {
        "permission_policy_hash",
        "sandbox_policy_hash",
        "secret_refs",
    }:
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest security fields are invalid"
        )
    permission_file = files_by_path.get("security/permissions.json")
    sandbox_file = files_by_path.get("security/sandbox-policy.json")
    if (
        permission_file is None
        or sandbox_file is None
        or security["permission_policy_hash"] != _hash_bytes(permission_file.content)
        or security["sandbox_policy_hash"] != _hash_bytes(sandbox_file.content)
    ):
        raise BundleCompilationError(
            "BUNDLE_SECURITY_HASH_MISMATCH", "Bundle security policy hash mismatch"
        )
    _verify_effective_policy_file(permission_file)
    secret_refs = security["secret_refs"]
    if not isinstance(secret_refs, list):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest Secret references are invalid"
        )
    typed_refs = cast(list[object], secret_refs)
    if len(typed_refs) > 100:
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest Secret references are invalid"
        )
    if (
        any(
            not isinstance(ref, str) or not ref.startswith("secret://")
            for ref in typed_refs
        )
        or typed_refs != sorted(typed_refs, key=str)
        or len(typed_refs) != len(set(cast(list[str], typed_refs)))
    ):
        raise BundleCompilationError(
            "MANIFEST_SCHEMA_INVALID", "Manifest Secret references are unsafe"
        )


def _verify_effective_policy_file(permission_file: BundleFile) -> None:
    try:
        payload_value: object = json.loads(permission_file.content)
    except (TypeError, ValueError) as error:
        raise BundleCompilationError(
            "BUNDLE_EFFECTIVE_POLICY_INVALID",
            "The effective policy file is not valid JSON.",
        ) from error
    if not isinstance(payload_value, dict):
        raise BundleCompilationError(
            "BUNDLE_EFFECTIVE_POLICY_INVALID",
            "The effective policy file shape is invalid.",
        )
    payload = cast(dict[str, object], payload_value)
    if set(payload) != {
        "schema_version",
        "effective_policy_hash",
        "effective_policy",
        "entries",
    }:
        raise BundleCompilationError(
            "BUNDLE_EFFECTIVE_POLICY_INVALID",
            "The effective policy file shape is invalid.",
        )
    effective_value = payload.get("effective_policy")
    effective = (
        cast(dict[str, object], effective_value)
        if isinstance(effective_value, dict)
        else None
    )
    if (
        payload.get("schema_version") != "bundle-permission-policy/v2"
        or effective is None
        or payload.get("effective_policy_hash") != _hash_json(effective)
        or effective.get("schema_version") != "effective-policy/v1"
        or effective.get("decision") not in {"ALLOW", "REQUIRE_APPROVAL"}
    ):
        raise BundleCompilationError(
            "BUNDLE_EFFECTIVE_POLICY_INVALID",
            "The effective policy snapshot is invalid or not admitted.",
        )


def _flatten_nodes(root: BundleAgentInput) -> tuple[BundleAgentInput, ...]:
    result: list[BundleAgentInput] = []
    active: set[UUID] = set()
    visited: set[UUID] = set()
    snapshots_by_agent: dict[UUID, UUID] = {}

    def visit(node: BundleAgentInput) -> None:
        if len(result) >= _MAX_GRAPH_NODES:
            raise BundleCompilationError(
                "BUNDLE_GRAPH_TOO_LARGE", "too many Agent nodes"
            )
        if node.agent_id in active:
            raise BundleCompilationError(
                "AGENT_GRAPH_RECURSIVE", "recursive Agent graph"
            )
        if node.snapshot_id in visited:
            return
        previous_snapshot = snapshots_by_agent.get(node.agent_id)
        if previous_snapshot is not None and previous_snapshot != node.snapshot_id:
            raise BundleCompilationError(
                "AGENT_GRAPH_AMBIGUOUS", "One Agent appears with multiple Snapshots."
            )
        snapshots_by_agent[node.agent_id] = node.snapshot_id
        active.add(node.agent_id)
        visited.add(node.snapshot_id)
        result.append(node)
        for child in sorted(node.children, key=lambda item: str(item.agent_id)):
            visit(child)
        active.remove(node.agent_id)

    visit(root)
    return tuple(result)


def _validate_nodes(nodes: tuple[BundleAgentInput, ...]) -> None:
    for node in nodes:
        if node.tenant_id != nodes[0].tenant_id:
            raise BundleCompilationError("TENANT_ISOLATION", "cross-tenant input")
        if node.snapshot_content.get("schema_version") != "agent-snapshot/v1":
            raise BundleCompilationError(
                "SNAPSHOT_SCHEMA_INVALID", "unsupported Snapshot"
            )
        agent_value = node.snapshot_content.get("agent")
        if (
            not isinstance(agent_value, dict)
            or agent_value.get("runtime_type") != "agentscope"
        ):
            raise BundleCompilationError(
                "RUNTIME_CAPABILITY_MISMATCH", "child runtime is not AgentScope"
            )
        if agent_value.get("id") != str(node.agent_id):
            raise BundleCompilationError(
                "SNAPSHOT_INPUT_MISMATCH", "Snapshot Agent identity mismatch"
            )
        if _hash_json(node.snapshot_content) != node.snapshot_hash:
            raise BundleCompilationError(
                "SNAPSHOT_HASH_MISMATCH", str(node.snapshot_id)
            )
        bindings = node.snapshot_content.get("bindings")
        if not isinstance(bindings, list):
            raise BundleCompilationError(
                "SNAPSHOT_SCHEMA_INVALID", "bindings must be a list"
            )
        sandbox_resources = [
            resource
            for resource in node.resources
            if resource.resource_type == "sandbox"
        ]
        if len(sandbox_resources) > 1:
            raise BundleCompilationError(
                "MULTIPLE_SANDBOX_POLICIES",
                "An Agent node cannot bind multiple sandbox profiles.",
            )
        typed_bindings = [
            cast(dict[str, JsonValue], item)
            for item in bindings
            if isinstance(item, dict)
        ]
        expected = {
            _binding_identity(item)
            for item in typed_bindings
            if item.get("resource_type") != "agent"
        }
        actual = {
            (resource.resource_type, resource.resource_id, resource.version_id)
            for resource in node.resources
        }
        if expected != actual:
            raise BundleCompilationError(
                "SNAPSHOT_INPUT_MISMATCH", str(node.snapshot_id)
            )
        for resource in node.resources:
            if canonical_content_hash(resource.content) != resource.content_hash:
                raise BundleCompilationError(
                    "RESOURCE_HASH_MISMATCH", str(resource.version_id)
                )
            if not _resource_content_matches_type(resource):
                raise BundleCompilationError(
                    "SNAPSHOT_INPUT_MISMATCH", str(resource.version_id)
                )
            binding = next(
                (
                    item
                    for item in typed_bindings
                    if item.get("resource_type") == resource.resource_type
                    and item.get("resource_id") == str(resource.resource_id)
                    and item.get("version_id") == str(resource.version_id)
                ),
                None,
            )
            if binding is None or binding.get("content_hash") != resource.content_hash:
                raise BundleCompilationError(
                    "SNAPSHOT_INPUT_MISMATCH", str(resource.version_id)
                )
            if resource.resource_type == "model":
                if (
                    resource.model_binding_snapshot_id is None
                    or resource.model_binding_snapshot_hash is None
                ):
                    raise BundleCompilationError(
                        "MODEL_BINDING_SNAPSHOT_REQUIRED", str(resource.version_id)
                    )
                if not _HASH_RE.fullmatch(resource.model_binding_snapshot_hash):
                    raise BundleCompilationError(
                        "MODEL_BINDING_SNAPSHOT_INVALID", str(resource.version_id)
                    )
                frozen = binding.get("model_binding_snapshot")
                if (
                    binding.get("binding_role") != resource.binding_role
                    or not isinstance(frozen, dict)
                    or frozen.get("id") != str(resource.model_binding_snapshot_id)
                    or frozen.get("content_hash")
                    != resource.model_binding_snapshot_hash
                ):
                    raise BundleCompilationError(
                        "MODEL_BINDING_SNAPSHOT_INVALID", str(resource.version_id)
                    )
            if (
                resource.resource_type == "mcp"
                and resource.mcp_capability_snapshot is not None
            ):
                capability = resource.mcp_capability_snapshot
                content = cast(ResourceContentMcp, resource.content)
                tool_names = {tool.name for tool in capability.tools}
                if (
                    capability.tenant_id != node.tenant_id
                    or capability.definition_id != resource.resource_id
                    or capability.published_version_id != resource.version_id
                    or capability.content_hash != resource.content_hash
                    or not _HASH_RE.fullmatch(capability.capability_hash)
                    or tuple(content.allowed_tools or ()) != capability.allowed_tools
                    or not set(capability.allowed_tools) <= tool_names
                    or any(
                        not _HASH_RE.fullmatch(tool.schema_hash)
                        for tool in capability.tools
                    )
                ):
                    raise BundleCompilationError(
                        "MCP_CAPABILITY_SNAPSHOT_INVALID", str(resource.version_id)
                    )
        child_bindings = [
            item for item in typed_bindings if item.get("resource_type") == "agent"
        ]
        if len(child_bindings) != len(node.children):
            raise BundleCompilationError(
                "SNAPSHOT_INPUT_MISMATCH", str(node.snapshot_id)
            )
        for child in node.children:
            if not any(
                item.get("resource_id") == str(child.agent_id)
                and item.get("version_id") == str(child.agent_version_id)
                and item.get("content_hash") == child.snapshot_hash
                and item.get("agent_snapshot_id") == str(child.snapshot_id)
                for item in child_bindings
            ):
                raise BundleCompilationError(
                    "SNAPSHOT_INPUT_MISMATCH", str(node.snapshot_id)
                )
        _validate_model_routing(node, typed_bindings)


def _binding_identity(binding: dict[str, JsonValue]) -> tuple[str, UUID, UUID]:
    try:
        return (
            str(binding.get("resource_type")),
            UUID(str(binding["resource_id"])),
            UUID(str(binding["version_id"])),
        )
    except (KeyError, ValueError) as exc:
        raise BundleCompilationError(
            "SNAPSHOT_SCHEMA_INVALID", "Snapshot binding identity is invalid"
        ) from exc


def _resource_content_matches_type(resource: BundleResourceInput) -> bool:
    expected_types = {
        "prompt": ResourceContentPrompt,
        "skill": ResourceContentSkill,
        "mcp": ResourceContentMcp,
        "model": ResourceContentModelConfig,
        "sandbox": ResourceContentSandboxProfile,
    }
    return isinstance(resource.content, expected_types[resource.resource_type])


def _validate_model_routing(
    node: BundleAgentInput, bindings: list[dict[str, JsonValue]]
) -> None:
    model_resources = [
        resource for resource in node.resources if resource.resource_type == "model"
    ]
    routing = node.snapshot_content.get("model_routing")
    if not model_resources:
        if routing is not None:
            raise BundleCompilationError(
                "MODEL_ROUTING_INVALID", "Model routing exists without Model bindings"
            )
        return
    if (
        not isinstance(routing, dict)
        or routing.get("schema_version") != "model-routing/v1"
    ):
        raise BundleCompilationError(
            "MODEL_ROUTING_INVALID", "Model routing is missing or unsupported"
        )
    routes = routing.get("routes")
    if not isinstance(routes, list):
        raise BundleCompilationError("MODEL_ROUTING_INVALID", "Routes must be an array")
    actual: set[tuple[str, str, str, str]] = set()
    route_roles: list[str] = []
    for route in routes:
        if not isinstance(route, dict):
            raise BundleCompilationError(
                "MODEL_ROUTING_INVALID", "A Model route must be an object"
            )
        route_roles.append(str(route.get("role")))
        actual.add(
            (
                str(route.get("role")),
                str(route.get("model_config_version_id")),
                str(route.get("model_binding_snapshot_id")),
                str(route.get("model_binding_snapshot_hash")),
            )
        )
    expected = {
        (
            str(resource.binding_role),
            str(resource.version_id),
            str(resource.model_binding_snapshot_id),
            str(resource.model_binding_snapshot_hash),
        )
        for resource in model_resources
    }
    if actual != expected:
        raise BundleCompilationError(
            "MODEL_ROUTING_INVALID", "Model routes do not match immutable bindings"
        )
    expected_roles = ["primary", "fallback_1", "fallback_2"][: len(routes)]
    if route_roles != expected_roles:
        raise BundleCompilationError(
            "MODEL_ROUTING_INVALID", "Model routes must use continuous fallback order"
        )
    fallback_codes = routing.get("fallback_error_codes")
    if (
        not isinstance(fallback_codes, list)
        or any(
            not isinstance(code, str)
            or code not in {"RATE_LIMITED", "PROVIDER_UNAVAILABLE"}
            for code in fallback_codes
        )
        or len(fallback_codes) != len(set(cast(list[str], fallback_codes)))
    ):
        raise BundleCompilationError(
            "MODEL_ROUTING_INVALID", "Fallback error codes are invalid"
        )
    model_binding_ids = {
        str(item.get("version_id"))
        for item in bindings
        if item.get("resource_type") == "model"
    }
    if model_binding_ids != {str(resource.version_id) for resource in model_resources}:
        raise BundleCompilationError(
            "MODEL_ROUTING_INVALID", "Model binding set is inconsistent"
        )


def _resource_sort_key(resource: BundleResourceInput) -> tuple[str, str, str]:
    return (resource.resource_type, str(resource.resource_id), str(resource.version_id))


def _resource_type(resource_type: str) -> str:
    return {"model_config": "model", "sandbox_profile": "sandbox"}.get(
        resource_type, resource_type
    )


def _policy_source(
    resource: BundleResourceInput, kind: Literal["sandbox", "skill", "mcp"]
) -> PolicyResourceSource:
    return PolicyResourceSource(
        kind=kind,
        resource_id=resource.resource_id,
        version_id=resource.version_id,
        content_hash=resource.content_hash,
    )


def _manifest_resource_type(resource_type: str) -> str:
    return _resource_type(resource_type)


def _agent_field(node: BundleAgentInput, field: str) -> JsonValue | None:
    agent = node.snapshot_content.get("agent")
    if not isinstance(agent, dict):
        return None
    value = agent.get(field)
    return cast(JsonValue, value)


def _artifact_map(
    artifacts: Iterable[BundleArtifactInput],
) -> dict[str, BundleArtifactInput]:
    result: dict[str, BundleArtifactInput] = {}
    for artifact in artifacts:
        if artifact.artifact_id in result:
            raise BundleCompilationError("DUPLICATE_ARTIFACT", artifact.artifact_id)
        result[artifact.artifact_id] = artifact
    return result


def _deduplicate_files(files: list[BundleFile]) -> list[BundleFile]:
    result: dict[str, BundleFile] = {}
    for file in files:
        _safe_relative_path(file.path)
        if len(file.content) > 1073741824 or not _MODE_RE.fullmatch(file.mode):
            raise BundleCompilationError("BUNDLE_FILE_INVALID", file.path)
        previous = result.get(file.path)
        if previous is not None:
            if previous.content != file.content or previous.mode != file.mode:
                raise BundleCompilationError("DUPLICATE_FILE_PATH", file.path)
            continue
        result[file.path] = file
    return list(result.values())


def _file_entry(file: BundleFile) -> dict[str, JsonValue]:
    return {
        "path": file.path,
        "hash": _hash_bytes(file.content),
        "size_bytes": len(file.content),
        "mode": file.mode,
        "source_type": file.source_type,
        "source_id": file.source_id,
    }


def _unique_payloads(
    values: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    result: dict[tuple[str, str, str], dict[str, object]] = {}
    for value in values:
        key = (
            str(value["resource_type"]),
            str(value["resource_id"]),
            str(value["version_id"]),
        )
        result[key] = value
    return list(result.values())


def _secret_refs(nodes: Iterable[BundleAgentInput]) -> set[str]:
    refs: set[str] = set()
    for node in nodes:
        for resource in node.resources:
            if resource.resource_type == "mcp":
                content = cast(ResourceContentMcp, resource.content)
                refs.update(content.secret_refs)
    invalid = sorted(ref for ref in refs if not ref.startswith("secret://"))
    if invalid:
        raise BundleCompilationError(
            "UNSAFE_SECRET_REFERENCE", "Bundle secret references must use secret://"
        )
    return refs


def _safe_relative_path(path: str) -> str:
    if (
        not path
        or len(path) > 1024
        or path.startswith("/")
        or "\x00" in path
        or "\\" in path
        or ".." in path.split("/")
    ):
        raise BundleCompilationError("UNSAFE_BUNDLE_PATH", path)
    normalized = posixpath.normpath(path)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized != path:
        raise BundleCompilationError("UNSAFE_BUNDLE_PATH", path)
    return normalized


def _verify_hash(content: bytes, expected: str, subject: str) -> None:
    actual = _hash_bytes(content)
    if actual != expected:
        raise BundleCompilationError("BUNDLE_ARTIFACT_HASH_MISMATCH", subject)


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _hash_json(value: object) -> str:
    return _hash_bytes(_json_bytes(value))


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
