"""Skill service publication evidence, authorization and replay tests."""

import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import yaml

from packages.application.public import (
    BaselineSkillSupplyChainScanner,
    CompositeResourceReferenceReader,
    RequestMetadata,
    SkillAccessResolver,
    SkillArtifactReader,
    SkillManagementService,
    SkillRegistry,
    SkillScanStore,
    SkillSupplyChainScanner,
)
from packages.contracts.generated.resource_content import (
    ResourceContentSkill,
    ResourceContentSkillFile,
    SkillManifest,
)
from packages.contracts.generated.resources_models import (
    ResourcePublishRequest,
    SkillCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    IdempotencyReplay,
    MutationOutcome,
    ResourceDefinitionRecord,
    ResourceVersionRecord,
    SkillArtifactPayload,
    SkillScanEvidenceRecord,
    SkillSupplyChainScanResult,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RESOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
SCAN_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 10, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-skill", trace_id="trace-skill")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="skill-admin",
        display_name="Skill Admin",
        platform_roles=frozenset(),
        auth_time=NOW,
    )


def access(*permissions: str) -> TenantAccess:
    return TenantAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.USER,
            subject_id=str(ACTOR_ID),
            membership_version=1,
            auth_time=NOW,
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset(permissions),
    )


def hash_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def skill_fixture() -> tuple[ResourceContentSkill, dict[UUID, SkillArtifactPayload]]:
    manifest_payload: dict[str, object] = {
        "apiVersion": "agent-platform/v1",
        "kind": "Skill",
        "metadata": {
            "name": "safe-skill",
            "version": "1.0.0",
            "displayName": "Safe Skill",
            "description": "Service fixture.",
        },
        "runtime": {
            "compatible": ["agentscope"],
            "minimumPlatformVersion": "1.0.0",
        },
        "entry": {"instructions": "SKILL.md", "command": None},
        "permissions": {
            "filesystem": {"read": [], "write": []},
            "network": {"allowDomains": [], "allowPorts": []},
            "tools": [],
            "secrets": [],
        },
        "dependencies": {"python": [], "system": [], "lockFile": None},
        "inputs": {"type": "object"},
        "outputs": {"type": "object"},
        "sandbox": {
            "imageDigest": "registry.example.test/skill@sha256:" + "a" * 64,
            "timeoutSeconds": 60,
            "riskLevel": "LOW",
            "requiresRunSandbox": False,
        },
        "tests": [],
    }
    files = {
        "SKILL.md": b"# Safe Skill\n",
        "manifest.yaml": yaml.safe_dump(manifest_payload, sort_keys=False).encode(),
    }
    artifacts: dict[UUID, SkillArtifactPayload] = {}
    declarations: list[ResourceContentSkillFile] = []
    for path, content in files.items():
        artifact_id = uuid4()
        digest = hash_bytes(content)
        artifacts[artifact_id] = SkillArtifactPayload(
            artifact_id=artifact_id,
            tenant_id=TENANT_ID,
            owner_user_id=ACTOR_ID,
            status="AVAILABLE",
            content_hash=digest,
            size_bytes=len(content),
            content_type="text/plain",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            content=content,
        )
        declarations.append(
            ResourceContentSkillFile(
                path=path,
                artifact_id=str(artifact_id),
                content_hash=digest,
            )
        )
    return (
        ResourceContentSkill(
            resource_type="skill",
            manifest=SkillManifest.model_validate(manifest_payload),
            files=declarations,
        ),
        artifacts,
    )


def definition(content: ResourceContentSkill, resource_version: int = 1):
    return ResourceDefinitionRecord(
        id=RESOURCE_ID,
        tenant_id=TENANT_ID,
        resource_type="skill",
        code="safe_skill",
        name="Safe Skill",
        description=None,
        owner_user_id=ACTOR_ID,
        visibility="tenant",
        content_schema_version="1.0",
        content=content,
        status="DRAFT",
        resource_version=resource_version,
        created_at=NOW,
        updated_at=NOW,
    )


def version(content: ResourceContentSkill) -> ResourceVersionRecord:
    return ResourceVersionRecord(
        id=VERSION_ID,
        tenant_id=TENANT_ID,
        definition_id=RESOURCE_ID,
        version_no=1,
        schema_version="1.0",
        content=content,
        content_hash="sha256:" + "b" * 64,
        release_note="release",
        status="PUBLISHED",
        published_at=NOW,
        published_by=ACTOR_ID,
    )


class ResolverStub:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del authenticated, metadata
        return self.tenant_access


class ArtifactReaderStub:
    def __init__(self, artifacts: dict[UUID, SkillArtifactPayload]) -> None:
        self.artifacts = artifacts
        self.calls = 0

    async def load_artifacts(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        self.calls += 1
        return self.artifacts


class RegistryStub:
    def __init__(self, content: ResourceContentSkill) -> None:
        self.content = content
        self.replay: IdempotencyReplay | None = None
        self.scan_attestation_id: UUID | None = None
        self.publish_calls = 0

    async def get_idempotency_replay(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return self.replay

    async def get_definition(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return definition(self.content)

    async def create_definition(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return MutationOutcome(value=definition(self.content))

    async def publish_version(self, context: TenantContext, **kwargs: object):
        del context
        self.publish_calls += 1
        self.scan_attestation_id = cast(UUID, kwargs["scan_attestation_id"])
        return MutationOutcome(value=version(self.content))


class ScanStoreStub:
    def __init__(self) -> None:
        self.result: SkillSupplyChainScanResult | None = None

    async def record_scan(self, context: TenantContext, **kwargs: object):
        del context
        self.result = cast(SkillSupplyChainScanResult, kwargs["result"])
        return SkillScanEvidenceRecord(
            id=SCAN_ID,
            tenant_id=TENANT_ID,
            definition_id=RESOURCE_ID,
            draft_resource_version=1,
            content_hash=cast(str, kwargs["content_hash"]),
            status=self.result.status,
            report_hash=self.result.report_hash,
            published_version_id=None,
            scanned_at=NOW,
            scanned_by=ACTOR_ID,
        )


class RejectingScanner:
    async def scan(self, context: TenantContext, package: object):
        del context, package
        return SkillSupplyChainScanResult(
            status="REJECTED",
            scanner_name="rejecting",
            scanner_version="1",
            policy_version="1",
            findings=(
                {
                    "code": "MALICIOUS_CONTENT",
                    "severity": "HIGH",
                    "path": "/SKILL.md",
                    "blocking": True,
                },
            ),
            report_hash="sha256:" + "c" * 64,
            sbom={},
            sbom_hash="sha256:" + "d" * 64,
            signature_status="NOT_PROVIDED",
            provenance_status="NOT_PROVIDED",
        )


class FailedScanner:
    async def scan(self, context: TenantContext, package: object):
        del context, package
        return SkillSupplyChainScanResult(
            status="FAILED",
            scanner_name="unavailable-scanner",
            scanner_version="1",
            policy_version="1",
            findings=(
                {
                    "code": "SCANNER_ENGINE_UNAVAILABLE",
                    "severity": "HIGH",
                    "path": "/",
                    "blocking": True,
                },
            ),
            report_hash="sha256:" + "e" * 64,
            sbom={},
            sbom_hash="sha256:" + "f" * 64,
            signature_status="NOT_PROVIDED",
            provenance_status="NOT_PROVIDED",
        )


def service(
    tenant_access: TenantAccess,
    registry: RegistryStub,
    reader: ArtifactReaderStub,
    scan_store: ScanStoreStub,
    scanner: object | None = None,
) -> SkillManagementService:
    return SkillManagementService(
        cast(SkillAccessResolver, ResolverStub(tenant_access)),
        cast(SkillRegistry, registry),
        CompositeResourceReferenceReader([]),
        cast(SkillArtifactReader, reader),
        cast(
            SkillSupplyChainScanner,
            scanner or BaselineSkillSupplyChainScanner(),
        ),
        cast(SkillScanStore, scan_store),
    )


@pytest.mark.asyncio
async def test_create_skill_validates_artifacts_and_requires_permission() -> None:
    content, artifacts = skill_fixture()
    reader = ArtifactReaderStub(artifacts)
    registry = RegistryStub(content)
    skill_service = service(access("skill:create"), registry, reader, ScanStoreStub())

    result, etag = await skill_service.create_skill(
        principal(),
        request=SkillCreateRequest(
            code="safe_skill",
            name="Safe Skill",
            description=None,
            visibility="tenant",
            content_schema_version="1.0",
            content=content,
        ),
        idempotency_key="skill-create-1",
        metadata=METADATA,
    )

    assert result.resource_type == "skill"
    assert etag == '"rv:1"'
    assert reader.calls == 1

    denied = service(access(), RegistryStub(content), reader, ScanStoreStub())
    with pytest.raises(PlatformError) as error:
        await denied.create_skill(
            principal(),
            request=SkillCreateRequest(
                code="safe_skill",
                name="Safe Skill",
                description=None,
                content_schema_version="1.0",
                content=content,
            ),
            idempotency_key="skill-create-2",
            metadata=METADATA,
        )
    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_publish_binds_passed_scan_evidence_to_registry() -> None:
    content, artifacts = skill_fixture()
    registry = RegistryStub(content)
    scan_store = ScanStoreStub()
    skill_service = service(
        access("skill:publish"),
        registry,
        ArtifactReaderStub(artifacts),
        scan_store,
    )

    result = await skill_service.publish_skill(
        principal(),
        resource_id=str(RESOURCE_ID),
        request=ResourcePublishRequest(
            expected_resource_version=1, release_note="release"
        ),
        idempotency_key="skill-publish-1",
        metadata=METADATA,
    )

    assert result.id == str(VERSION_ID)
    assert scan_store.result is not None
    assert scan_store.result.status == "PASSED"
    assert registry.scan_attestation_id == SCAN_ID


@pytest.mark.asyncio
async def test_rejected_scan_is_recorded_and_never_reaches_registry_publish() -> None:
    content, artifacts = skill_fixture()
    registry = RegistryStub(content)
    scan_store = ScanStoreStub()
    skill_service = service(
        access("skill:publish"),
        registry,
        ArtifactReaderStub(artifacts),
        scan_store,
        RejectingScanner(),
    )

    with pytest.raises(PlatformError) as error:
        await skill_service.publish_skill(
            principal(),
            resource_id=str(RESOURCE_ID),
            request=ResourcePublishRequest(
                expected_resource_version=1, release_note="release"
            ),
            idempotency_key="skill-publish-2",
            metadata=METADATA,
        )

    assert error.value.code == "RESOURCE_STATE_CONFLICT"
    assert scan_store.result is not None
    assert scan_store.result.status == "REJECTED"
    assert registry.publish_calls == 0


@pytest.mark.asyncio
async def test_failed_scan_is_recorded_and_returns_dependency_unavailable() -> None:
    content, artifacts = skill_fixture()
    registry = RegistryStub(content)
    scan_store = ScanStoreStub()
    skill_service = service(
        access("skill:publish"),
        registry,
        ArtifactReaderStub(artifacts),
        scan_store,
        FailedScanner(),
    )

    with pytest.raises(PlatformError) as error:
        await skill_service.publish_skill(
            principal(),
            resource_id=str(RESOURCE_ID),
            request=ResourcePublishRequest(
                expected_resource_version=1, release_note="release"
            ),
            idempotency_key="skill-publish-failed",
            metadata=METADATA,
        )

    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert scan_store.result is not None
    assert scan_store.result.status == "FAILED"
    assert registry.publish_calls == 0


@pytest.mark.asyncio
async def test_completed_publish_replay_does_not_reopen_or_rescan_artifacts() -> None:
    content, _ = skill_fixture()
    registry = RegistryStub(content)
    registry.replay = IdempotencyReplay(
        response_status=201,
        response_body={
            "id": str(VERSION_ID),
            "definition_id": str(RESOURCE_ID),
            "version_no": 1,
            "content_hash": "sha256:" + "b" * 64,
            "release_note": "release",
            "published_at": NOW.isoformat(),
        },
        response_etag=None,
    )
    reader = ArtifactReaderStub({})
    skill_service = service(access("skill:publish"), registry, reader, ScanStoreStub())

    result = await skill_service.publish_skill(
        principal(),
        resource_id=str(RESOURCE_ID),
        request=ResourcePublishRequest(
            expected_resource_version=1, release_note="release"
        ),
        idempotency_key="skill-publish-replay",
        metadata=METADATA,
    )

    assert result.id == str(VERSION_ID)
    assert reader.calls == 0
    assert registry.publish_calls == 0
