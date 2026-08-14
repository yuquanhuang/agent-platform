"""AgentScope Bundle compiler contract and security tests."""

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue

from packages.contracts.generated.resource_content import (
    ResourceContentMcp,
    ResourceContentModelConfig,
    ResourceContentPrompt,
    ResourceContentSandboxProfile,
    ResourceContentSkill,
)
from packages.domain.public import (
    BundleAgentInput,
    BundleArtifactInput,
    BundleCompilationError,
    BundleResourceInput,
    McpCapabilitySnapshotInput,
    McpDiscoveredTool,
    compile_agentscope_bundle,
    verify_compiled_bundle,
)
from packages.domain.resources import canonical_content_hash

PROJECT_ROOT = Path(__file__).resolve().parents[4]
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
AGENT_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_VERSION_ID = UUID("33333333-3333-4333-8333-333333333333")
SNAPSHOT_ID = UUID("44444444-4444-4444-8444-444444444444")
PROMPT_ID = UUID("55555555-5555-4555-8555-555555555555")
PROMPT_VERSION_ID = UUID("66666666-6666-4666-8666-666666666666")
MODEL_ID = UUID("77777777-7777-4777-8777-777777777777")
MODEL_VERSION_ID = UUID("88888888-8888-4888-8888-888888888888")
MODEL_SNAPSHOT_ID = UUID("99999999-9999-4999-8999-999999999999")
SANDBOX_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SANDBOX_VERSION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SKILL_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SKILL_VERSION_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
MCP_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
MCP_VERSION_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
NOW = datetime(2026, 8, 7, 8, tzinfo=UTC)


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _prompt() -> ResourceContentPrompt:
    return ResourceContentPrompt(
        resource_type="prompt",
        template="You are a careful support agent.",
        variables=[],
        language="en",
        compiler_policy_version="1",
    )


def _model() -> ResourceContentModelConfig:
    return ResourceContentModelConfig(
        resource_type="model_config",
        provider_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        model_id="gpt-test",
        capabilities=["stream", "tools"],
        default_parameters={"temperature": 0.1},
        max_context_tokens=8192,
        rate_limit_rpm=60,
        max_output_tokens=None,
        max_reasoning_tokens=None,
        counter_profile_id=None,
        counter_profile_version=None,
        counter_profile_hash=None,
        billing_semantics_version=None,
    )


def _sandbox() -> ResourceContentSandboxProfile:
    return ResourceContentSandboxProfile.model_validate(
        {
            "resource_type": "sandbox_profile",
            "policy": {
                "schema_version": "1.0",
                "scope": "run",
                "image_digest": f"registry.example/runtime@sha256:{'d' * 64}",
                "cpu_limit": 1,
                "memory_mb": 512,
                "disk_mb": 1024,
                "pids_limit": 64,
                "timeout_seconds": 600,
                "run_as_non_root": True,
                "readonly_root_filesystem": True,
                "network": {
                    "mode": "none",
                    "allow_domains": [],
                    "allow_ports": [],
                    "deny_private_networks": True,
                },
                "filesystem": {
                    "read_patterns": ["/bundle/**"],
                    "write_patterns": ["/workspace/**"],
                    "max_files": 1000,
                    "max_file_bytes": 10485760,
                    "allow_symlinks": False,
                    "allow_device_files": False,
                },
                "process": {
                    "allowed_executables": ["python"],
                    "shell_allowed": False,
                    "max_processes": 32,
                },
                "artifacts": {
                    "allow_export": True,
                    "max_artifacts": 20,
                    "max_total_bytes": 104857600,
                    "allowed_content_types": ["text/plain"],
                },
            },
        }
    )


def _root(*, include_sandbox: bool = True) -> BundleAgentInput:
    prompt = _prompt()
    model = _model()
    sandbox = _sandbox()
    resources = [
        BundleResourceInput(
            resource_type="prompt",
            resource_id=PROMPT_ID,
            version_id=PROMPT_VERSION_ID,
            content_hash=canonical_content_hash(prompt),
            content=prompt,
        ),
        BundleResourceInput(
            resource_type="model",
            resource_id=MODEL_ID,
            version_id=MODEL_VERSION_ID,
            content_hash=canonical_content_hash(model),
            content=model,
            binding_role="primary",
            model_binding_snapshot_id=MODEL_SNAPSHOT_ID,
            model_binding_snapshot_hash="sha256:" + "e" * 64,
        ),
    ]
    if include_sandbox:
        resources.append(
            BundleResourceInput(
                resource_type="sandbox",
                resource_id=SANDBOX_ID,
                version_id=SANDBOX_VERSION_ID,
                content_hash=canonical_content_hash(sandbox),
                content=sandbox,
            )
        )
    bindings: list[dict[str, JsonValue]] = [
        {
            "resource_type": resource.resource_type,
            "resource_id": str(resource.resource_id),
            "version_id": str(resource.version_id),
            "version_no": 1,
            "schema_version": "1.0",
            "content_hash": resource.content_hash,
        }
        for resource in resources
    ]
    model_binding = next(
        binding for binding in bindings if binding["resource_type"] == "model"
    )
    model_binding.update(
        {
            "binding_role": "primary",
            "model_binding_snapshot": {
                "id": str(MODEL_SNAPSHOT_ID),
                "content_hash": "sha256:" + "e" * 64,
            },
        }
    )
    content = cast(
        dict[str, JsonValue],
        {
            "schema_version": "agent-snapshot/v1",
            "agent": {
                "id": str(AGENT_ID),
                "code": "support_agent",
                "name": "Support Agent",
                "description": None,
                "runtime_type": "agentscope",
                "visibility": "tenant",
                "tags": ["support"],
                "default_language": "en",
            },
            "source": {"draft_resource_version": 2},
            "bindings": bindings,
            "model_routing": {
                "schema_version": "model-routing/v1",
                "fallback_error_codes": [],
                "routes": [
                    {
                        "role": "primary",
                        "model_config_version_id": str(MODEL_VERSION_ID),
                        "model_binding_snapshot_id": str(MODEL_SNAPSHOT_ID),
                        "model_binding_snapshot_hash": "sha256:" + "e" * 64,
                    }
                ],
            },
        },
    )
    return BundleAgentInput(
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        agent_version_id=AGENT_VERSION_ID,
        snapshot_id=SNAPSHOT_ID,
        snapshot_hash=_hash_json(content),
        snapshot_content=content,
        created_at=NOW,
        resources=tuple(resources),
    )


def test_compiler_is_deterministic_and_matches_frozen_manifest_schema() -> None:
    first = compile_agentscope_bundle(_root())
    second = compile_agentscope_bundle(_root())

    assert first == second
    assert first.bundle_id.startswith("bundle_")
    assert first.content_hash == first.manifest["content_hash"]
    assert first.file("prompt/system.md").content == (
        b"You are a careful support agent."
    )
    assert json.loads(first.file("runtime.yaml").content)["runtime_type"] == (
        "agentscope"
    )
    manifest_files = cast(
        list[dict[str, object]], cast(object, first.manifest["files"])
    )
    assert "manifest.json" not in {str(entry["path"]) for entry in manifest_files}
    assert {file.path for file in first.files} == {
        "file-manifest.json",
        "manifest.json",
        "prompt/system.md",
        "runtime.yaml",
        "security/permissions.json",
        "security/sandbox-policy.json",
    }

    schema = json.loads(
        (
            PROJECT_ROOT
            / "docs"
            / "agent-platform"
            / "schemas"
            / "bundle-manifest-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(
        cast(dict[str, object], schema), format_checker=FormatChecker()
    )
    validate = cast(
        Callable[[object], None],
        getattr(cast(object, validator), "validate"),  # noqa: B009
    )
    validate(first.manifest)


def test_timestamp_does_not_change_bundle_identity_or_content_hash() -> None:
    first = compile_agentscope_bundle(_root())
    later = compile_agentscope_bundle(
        replace(_root(), created_at=datetime(2026, 8, 8, tzinfo=UTC))
    )

    assert first.bundle_id == later.bundle_id
    assert first.content_hash == later.content_hash
    assert first.manifest["created_at"] != later.manifest["created_at"]


def test_verifier_rejects_tampered_file_bytes() -> None:
    bundle = compile_agentscope_bundle(_root())
    tampered_files = tuple(
        replace(file, content=b"tampered") if file.path == "prompt/system.md" else file
        for file in bundle.files
    )

    with pytest.raises(BundleCompilationError) as error:
        verify_compiled_bundle(replace(bundle, files=tampered_files))

    assert error.value.code == "BUNDLE_FILE_HASH_MISMATCH"


def test_compiler_fails_closed_without_immutable_sandbox_policy() -> None:
    with pytest.raises(BundleCompilationError) as error:
        compile_agentscope_bundle(_root(include_sandbox=False))

    assert error.value.code == "SANDBOX_POLICY_REQUIRED"


def test_compiler_rejects_resource_content_hash_drift() -> None:
    root = _root()
    prompt = root.resources[0]
    drifted = replace(
        root,
        resources=(
            replace(prompt, content_hash="sha256:" + "0" * 64),
            *root.resources[1:],
        ),
    )

    with pytest.raises(BundleCompilationError) as error:
        compile_agentscope_bundle(drifted)

    assert error.value.code in {"SNAPSHOT_INPUT_MISMATCH", "RESOURCE_HASH_MISMATCH"}


def test_compiler_packages_skill_artifacts_and_mcp_secret_references() -> None:
    skill_md = b"# Search\nUse the approved search tool.\n"
    manifest_yaml = b"apiVersion: agent-platform/v1\nkind: Skill\n"
    skill = ResourceContentSkill.model_validate(
        {
            "resource_type": "skill",
            "manifest": {
                "apiVersion": "agent-platform/v1",
                "kind": "Skill",
                "metadata": {
                    "name": "web-search",
                    "version": "1.0.0",
                    "displayName": "Web Search",
                    "description": "Search approved public sources.",
                },
                "runtime": {
                    "compatible": ["agentscope"],
                    "minimumPlatformVersion": "1.0.0",
                },
                "entry": {"instructions": "SKILL.md"},
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": {"allowDomains": [], "allowPorts": []},
                    "tools": ["search.query"],
                    "secrets": ["secret-purpose:search-api"],
                },
                "dependencies": {"python": [], "system": []},
                "inputs": {"type": "object"},
                "outputs": {"type": "object"},
                "sandbox": {
                    "imageDigest": f"registry.example/runtime@sha256:{'d' * 64}",
                    "timeoutSeconds": 60,
                    "riskLevel": "LOW",
                },
            },
            "files": [
                {
                    "path": "SKILL.md",
                    "artifact_id": "artifact-skill-md",
                    "content_hash": f"sha256:{hashlib.sha256(skill_md).hexdigest()}",
                },
                {
                    "path": "manifest.yaml",
                    "artifact_id": "artifact-manifest",
                    "content_hash": f"sha256:{hashlib.sha256(manifest_yaml).hexdigest()}",
                },
            ],
        }
    )
    mcp = ResourceContentMcp(
        resource_type="mcp",
        transport="streamable_http",
        endpoint="https://mcp.example/v1",
        header_templates={"Authorization": "Bearer ${SEARCH_TOKEN}"},
        secret_refs=[f"secret://tenant/{TENANT_ID}/mcp/search"],
        timeout_seconds=30,
        allowed_tools=["search.query"],
    )
    mcp_capability = McpCapabilitySnapshotInput(
        id=UUID("12121212-1212-4121-8121-121212121212"),
        tenant_id=TENANT_ID,
        definition_id=MCP_ID,
        published_version_id=MCP_VERSION_ID,
        content_hash=canonical_content_hash(mcp),
        capability_hash="sha256:" + "1" * 64,
        protocol_version="2025-06-18",
        server_name="search-mcp",
        server_version="1.0.0",
        allowed_tools=("search.query",),
        tools=(
            McpDiscoveredTool(
                name="search.query",
                description="Search approved public sources.",
                input_schema={"type": "object"},
                output_schema=None,
                schema_hash="sha256:" + "2" * 64,
                risk_level="MEDIUM",
            ),
        ),
    )
    root = _root()
    content = deepcopy(root.snapshot_content)
    bindings_value = content["bindings"]
    assert isinstance(bindings_value, list)
    resources = (
        *root.resources,
        BundleResourceInput(
            resource_type="skill",
            resource_id=SKILL_ID,
            version_id=SKILL_VERSION_ID,
            content_hash=canonical_content_hash(skill),
            content=skill,
        ),
        BundleResourceInput(
            resource_type="mcp",
            resource_id=MCP_ID,
            version_id=MCP_VERSION_ID,
            content_hash=canonical_content_hash(mcp),
            content=mcp,
            mcp_capability_snapshot=mcp_capability,
        ),
    )
    for resource in resources[-2:]:
        bindings_value.append(
            {
                "resource_type": resource.resource_type,
                "resource_id": str(resource.resource_id),
                "version_id": str(resource.version_id),
                "version_no": 1,
                "schema_version": "1.0",
                "content_hash": resource.content_hash,
            }
        )
    root = replace(
        root,
        snapshot_content=content,
        snapshot_hash=_hash_json(content),
        resources=resources,
    )

    bundle = compile_agentscope_bundle(
        root,
        (
            BundleArtifactInput(
                artifact_id="artifact-skill-md",
                content_hash=f"sha256:{hashlib.sha256(skill_md).hexdigest()}",
                content=skill_md,
            ),
            BundleArtifactInput(
                artifact_id="artifact-manifest",
                content_hash=f"sha256:{hashlib.sha256(manifest_yaml).hexdigest()}",
                content=manifest_yaml,
            ),
        ),
    )

    assert bundle.file("skills/web-search/SKILL.md").content == skill_md
    assert bundle.file("skills/web-search/manifest.yaml").content == manifest_yaml
    assert bundle.file(f"mcp/{MCP_ID}.json").content
    security = cast(dict[str, JsonValue], bundle.manifest["security"])
    assert security["secret_refs"] == [f"secret://tenant/{TENANT_ID}/mcp/search"]
    mcp_file = json.loads(bundle.file(f"mcp/{MCP_ID}.json").content)
    assert mcp_file["capability_evidence"]["capability_hash"] == ("sha256:" + "1" * 64)
    assert mcp_file["capability_evidence"]["tools"][0]["schema_hash"] == (
        "sha256:" + "2" * 64
    )
    assert (
        "secret-purpose:search-api"
        in bundle.file("security/permissions.json").content.decode()
    )
    permissions = json.loads(bundle.file("security/permissions.json").content)
    assert permissions["schema_version"] == "bundle-permission-policy/v2"
    assert permissions["effective_policy"]["decision"] == "ALLOW"
    assert permissions["effective_policy"]["maximum_risk_level"] == "MEDIUM"

    high_resources = tuple(
        (
            replace(
                resource,
                mcp_capability_snapshot=replace(
                    resource.mcp_capability_snapshot,
                    tools=tuple(
                        replace(tool, risk_level="HIGH")
                        for tool in resource.mcp_capability_snapshot.tools
                    ),
                ),
            )
            if resource.resource_type == "mcp"
            and resource.mcp_capability_snapshot is not None
            else resource
        )
        for resource in resources
    )
    high_bundle = compile_agentscope_bundle(
        replace(root, resources=high_resources),
        (
            BundleArtifactInput(
                artifact_id="artifact-skill-md",
                content_hash=f"sha256:{hashlib.sha256(skill_md).hexdigest()}",
                content=skill_md,
            ),
            BundleArtifactInput(
                artifact_id="artifact-manifest",
                content_hash=f"sha256:{hashlib.sha256(manifest_yaml).hexdigest()}",
                content=manifest_yaml,
            ),
        ),
    )
    high_permissions = json.loads(high_bundle.file("security/permissions.json").content)
    assert high_permissions["effective_policy"]["decision"] == "REQUIRE_APPROVAL"
    assert high_permissions["effective_policy"]["maximum_risk_level"] == "HIGH"
    verify_compiled_bundle(high_bundle)
