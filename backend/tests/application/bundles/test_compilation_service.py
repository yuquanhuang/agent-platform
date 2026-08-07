"""Immutable input loading tests for AgentScope Bundle compilation."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from packages.application.bundles import (
    AgentScopeBundleCompilationService,
    BundleArtifactReader,
    BundleInputReader,
)
from packages.application.publishing import SnapshotReader
from packages.contracts.generated.resource_content import (
    ResourceContentModelConfig,
    ResourceContentPrompt,
    ResourceContentSandboxProfile,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import (
    AgentSnapshotRecord,
    AgentVersionRecord,
    BundleCompilationError,
    BundleModelBindingSnapshotInput,
    ResourceVersionRecord,
    canonical_content_hash,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
ACTOR_ID = UUID("33333333-3333-4333-8333-333333333333")
AGENT_ID = UUID("44444444-4444-4444-8444-444444444444")
AGENT_VERSION_ID = UUID("55555555-5555-4555-8555-555555555555")
SNAPSHOT_ID = UUID("66666666-6666-4666-8666-666666666666")
PROMPT_ID = UUID("77777777-7777-4777-8777-777777777777")
PROMPT_VERSION_ID = UUID("88888888-8888-4888-8888-888888888888")
MODEL_ID = UUID("99999999-9999-4999-8999-999999999999")
MODEL_VERSION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MODEL_SNAPSHOT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SANDBOX_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SANDBOX_VERSION_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
NOW = datetime(2026, 8, 7, 8, tzinfo=UTC)


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR_ID),
        membership_version=1,
        auth_time=NOW,
        request_id="req-bundle",
        trace_id="trace-bundle",
    )


def _resources() -> tuple[ResourceVersionRecord, ...]:
    prompt = ResourceContentPrompt(
        resource_type="prompt",
        template="System prompt",
        variables=[],
        language="en",
        compiler_policy_version="1",
    )
    model = ResourceContentModelConfig(
        resource_type="model_config",
        provider_id=str(UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")),
        model_id="gateway-model",
        capabilities=["stream"],
        default_parameters={},
        max_context_tokens=None,
        rate_limit_rpm=None,
    )
    sandbox = ResourceContentSandboxProfile.model_validate(
        {
            "resource_type": "sandbox_profile",
            "policy": {
                "schema_version": "1.0",
                "scope": "run",
                "image_digest": f"registry.example/sandbox@sha256:{'f' * 64}",
                "cpu_limit": 1,
                "memory_mb": 512,
                "disk_mb": 512,
                "pids_limit": 32,
                "timeout_seconds": 300,
                "network": {
                    "mode": "none",
                    "allow_domains": [],
                    "allow_ports": [],
                    "deny_private_networks": True,
                },
                "filesystem": {
                    "read_patterns": ["/bundle/**"],
                    "write_patterns": ["/workspace/**"],
                    "max_files": 100,
                    "max_file_bytes": 1048576,
                },
                "process": {
                    "allowed_executables": ["python"],
                    "shell_allowed": False,
                    "max_processes": 16,
                },
                "artifacts": {
                    "allow_export": False,
                    "max_artifacts": 0,
                    "max_total_bytes": 0,
                    "allowed_content_types": [],
                },
            },
        }
    )
    values = (
        (PROMPT_ID, PROMPT_VERSION_ID, prompt),
        (MODEL_ID, MODEL_VERSION_ID, model),
        (SANDBOX_ID, SANDBOX_VERSION_ID, sandbox),
    )
    return tuple(
        ResourceVersionRecord(
            id=version_id,
            tenant_id=TENANT_ID,
            definition_id=resource_id,
            version_no=1,
            schema_version="1.0",
            content=content,
            content_hash=canonical_content_hash(content),
            release_note="Published",
            status="PUBLISHED",
            published_at=NOW,
            published_by=ACTOR_ID,
        )
        for resource_id, version_id, content in values
    )


def _snapshot(resources: tuple[ResourceVersionRecord, ...]) -> AgentSnapshotRecord:
    bindings: list[dict[str, JsonValue]] = []
    type_by_id = {
        PROMPT_ID: "prompt",
        MODEL_ID: "model",
        SANDBOX_ID: "sandbox",
    }
    for resource in resources:
        binding: dict[str, JsonValue] = {
            "resource_type": type_by_id[resource.definition_id],
            "resource_id": str(resource.definition_id),
            "version_id": str(resource.id),
            "version_no": resource.version_no,
            "schema_version": resource.schema_version,
            "content_hash": resource.content_hash,
        }
        if resource.id == MODEL_VERSION_ID:
            binding.update(
                {
                    "binding_role": "primary",
                    "model_binding_snapshot": {
                        "id": str(MODEL_SNAPSHOT_ID),
                        "content_hash": "sha256:" + "1" * 64,
                    },
                }
            )
        bindings.append(binding)
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
                "tags": [],
                "default_language": "en",
            },
            "source": {"draft_resource_version": 1},
            "bindings": bindings,
            "model_routing": {
                "schema_version": "model-routing/v1",
                "fallback_error_codes": [],
                "routes": [
                    {
                        "role": "primary",
                        "model_config_version_id": str(MODEL_VERSION_ID),
                        "model_binding_snapshot_id": str(MODEL_SNAPSHOT_ID),
                        "model_binding_snapshot_hash": "sha256:" + "1" * 64,
                    }
                ],
            },
        },
    )
    return AgentSnapshotRecord(
        id=SNAPSHOT_ID,
        tenant_id=TENANT_ID,
        agent_version_id=AGENT_VERSION_ID,
        schema_version="agent-snapshot/v1",
        content=content,
        content_hash=_hash_json(content),
        compiler_input_hash="sha256:" + "2" * 64,
        created_at=NOW,
        created_by=ACTOR_ID,
    )


class SnapshotReaderStub:
    def __init__(self, snapshot: AgentSnapshotRecord) -> None:
        self.snapshot = snapshot
        self.version = AgentVersionRecord(
            id=AGENT_VERSION_ID,
            tenant_id=snapshot.tenant_id,
            agent_id=AGENT_ID,
            version_no=1,
            created_from_version_id=None,
            release_note="Published",
            created_at=NOW,
            created_by=ACTOR_ID,
        )

    async def get_snapshot(self, context: TenantContext, *, snapshot_id: UUID):
        del context
        return self.snapshot if snapshot_id == self.snapshot.id else None

    async def get_version(self, context: TenantContext, *, agent_version_id: UUID):
        del context
        return self.version if agent_version_id == self.version.id else None


class InputReaderStub:
    def __init__(self, resources: tuple[ResourceVersionRecord, ...]) -> None:
        self.resources = {record.id: record for record in resources}

    async def get_resource_version(
        self,
        context: TenantContext,
        *,
        resource_id: UUID,
        version_id: UUID,
    ):
        del context
        record = self.resources.get(version_id)
        return (
            record
            if record is not None and record.definition_id == resource_id
            else None
        )

    async def get_model_binding_snapshot(
        self, context: TenantContext, *, snapshot_id: UUID
    ):
        del context
        if snapshot_id != MODEL_SNAPSHOT_ID:
            return None
        return BundleModelBindingSnapshotInput(
            id=MODEL_SNAPSHOT_ID,
            tenant_id=TENANT_ID,
            model_config_version_id=MODEL_VERSION_ID,
            snapshot_hash="sha256:" + "1" * 64,
        )


class ArtifactReaderStub:
    async def get_artifact(self, context: TenantContext, *, artifact_id: str):
        del context, artifact_id
        raise AssertionError("No Skill Artifact should be read in this scenario")


@pytest.mark.asyncio
async def test_service_compiles_only_from_snapshot_and_immutable_versions() -> None:
    resources = _resources()
    snapshot = _snapshot(resources)
    service = AgentScopeBundleCompilationService(
        cast(SnapshotReader, SnapshotReaderStub(snapshot)),
        cast(BundleInputReader, InputReaderStub(resources)),
        cast(BundleArtifactReader, ArtifactReaderStub()),
    )

    bundle = await service.compile_bundle(_context(), snapshot_id=SNAPSHOT_ID)

    assert bundle.snapshot_id == SNAPSHOT_ID
    assert bundle.file("prompt/system.md").content == b"System prompt"
    assert bundle.manifest["bindings"] is not None


@pytest.mark.asyncio
async def test_service_rejects_cross_tenant_snapshot_fact() -> None:
    resources = _resources()
    snapshot = _snapshot(resources)
    foreign = AgentSnapshotRecord(
        id=snapshot.id,
        tenant_id=OTHER_TENANT_ID,
        agent_version_id=snapshot.agent_version_id,
        schema_version=snapshot.schema_version,
        content=snapshot.content,
        content_hash=snapshot.content_hash,
        compiler_input_hash=snapshot.compiler_input_hash,
        created_at=snapshot.created_at,
        created_by=snapshot.created_by,
    )
    service = AgentScopeBundleCompilationService(
        cast(SnapshotReader, SnapshotReaderStub(foreign)),
        cast(BundleInputReader, InputReaderStub(resources)),
        cast(BundleArtifactReader, ArtifactReaderStub()),
    )

    with pytest.raises(BundleCompilationError) as error:
        await service.compile_bundle(_context(), snapshot_id=SNAPSHOT_ID)

    assert error.value.code == "TENANT_ISOLATION"


@pytest.mark.asyncio
async def test_service_rejects_missing_immutable_resource_version() -> None:
    resources = _resources()
    snapshot = _snapshot(resources)
    service = AgentScopeBundleCompilationService(
        cast(SnapshotReader, SnapshotReaderStub(snapshot)),
        cast(BundleInputReader, InputReaderStub(resources[1:])),
        cast(BundleArtifactReader, ArtifactReaderStub()),
    )

    with pytest.raises(BundleCompilationError) as error:
        await service.compile_bundle(_context(), snapshot_id=SNAPSHOT_ID)

    assert error.value.code == "RESOURCE_VERSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_service_reproduces_snapshot_after_version_is_disabled() -> None:
    resources = _resources()
    snapshot = _snapshot(resources)
    disabled_resources = tuple(
        replace(record, status="DISABLED") for record in resources
    )
    service = AgentScopeBundleCompilationService(
        cast(SnapshotReader, SnapshotReaderStub(snapshot)),
        cast(BundleInputReader, InputReaderStub(disabled_resources)),
        cast(BundleArtifactReader, ArtifactReaderStub()),
    )

    bundle = await service.compile_bundle(_context(), snapshot_id=SNAPSHOT_ID)

    assert bundle.snapshot_id == SNAPSHOT_ID
    assert bundle.file("prompt/system.md").content == b"System prompt"
