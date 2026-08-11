"""Skill import validation and baseline supply-chain scanner tests."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import JsonValue

from packages.application.public import (
    BaselineSkillSupplyChainScanner,
    SkillArtifactReader,
    validate_skill_package,
)
from packages.contracts.generated.resource_content import (
    ResourceContentSkill,
    ResourceContentSkillFile,
    SkillManifest,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import SkillArtifactPayload

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR_ID),
        membership_version=1,
        auth_time=NOW,
        request_id="req-skill-package",
        trace_id="trace-skill-package",
    )


def manifest_payload() -> dict[str, object]:
    return {
        "apiVersion": "agent-platform/v1",
        "kind": "Skill",
        "metadata": {
            "name": "safe-skill",
            "version": "1.0.0",
            "displayName": "Safe Skill",
            "description": "A deterministic test Skill.",
            "license": "Apache-2.0",
            "authors": ["Agent Platform"],
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
        "inputs": {"type": "object", "additionalProperties": False},
        "outputs": {"type": "object", "additionalProperties": False},
        "sandbox": {
            "imageDigest": "registry.example.test/skill@sha256:" + "a" * 64,
            "timeoutSeconds": 60,
            "riskLevel": "LOW",
            "requiresRunSandbox": False,
        },
        "tests": [],
    }


def sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def package_content(
    manifest: dict[str, object] | None = None,
    *,
    manifest_bytes: bytes | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> tuple[ResourceContentSkill, dict[UUID, SkillArtifactPayload]]:
    payload = manifest or manifest_payload()
    rendered = manifest_bytes or yaml.safe_dump(payload, sort_keys=False).encode()
    files = {"SKILL.md": b"# Safe Skill\n", "manifest.yaml": rendered}
    files.update(extra_files or {})
    artifacts: dict[UUID, SkillArtifactPayload] = {}
    declarations: list[ResourceContentSkillFile] = []
    for path, content in files.items():
        artifact_id = uuid4()
        content_hash = sha256(content)
        artifacts[artifact_id] = SkillArtifactPayload(
            artifact_id=artifact_id,
            tenant_id=TENANT_ID,
            owner_user_id=ACTOR_ID,
            status="AVAILABLE",
            content_hash=content_hash,
            size_bytes=len(content),
            content_type="text/plain",
            expires_at=NOW + timedelta(days=1),
            content=content,
        )
        declarations.append(
            ResourceContentSkillFile(
                path=path,
                artifact_id=str(artifact_id),
                content_hash=content_hash,
            )
        )
    return (
        ResourceContentSkill(
            resource_type="skill",
            manifest=SkillManifest.model_validate(payload),
            files=declarations,
        ),
        artifacts,
    )


class ReaderStub:
    def __init__(self, artifacts: dict[UUID, SkillArtifactPayload]) -> None:
        self.artifacts = artifacts

    async def load_artifacts(
        self,
        tenant_context: TenantContext,
        *,
        owner_user_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> dict[UUID, SkillArtifactPayload]:
        del tenant_context, owner_user_id
        return {
            artifact_id: self.artifacts[artifact_id]
            for artifact_id in artifact_ids
            if artifact_id in self.artifacts
        }


async def validated(
    content: ResourceContentSkill,
    artifacts: dict[UUID, SkillArtifactPayload],
):
    return await validate_skill_package(
        context(),
        owner_user_id=ACTOR_ID,
        content=content,
        artifact_reader=cast(SkillArtifactReader, ReaderStub(artifacts)),
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_valid_package_binds_exact_artifact_bytes_and_scans_passed() -> None:
    content, artifacts = package_content()

    package = await validated(content, artifacts)
    result = await BaselineSkillSupplyChainScanner().scan(context(), package)

    assert result.status == "PASSED"
    expected_report: dict[str, JsonValue] = {
        "scanner": "agent-platform-skill-baseline",
        "scanner_version": "1.0.0",
        "policy_version": "skill-supply-chain-v1",
        "scan_profile": "STATIC_BASELINE",
        "status": "PASSED",
        "content_hash": package.content_hash,
        "sbom_hash": result.sbom_hash,
        "signature_status": "NOT_PROVIDED",
        "provenance_status": "NOT_PROVIDED",
        "license_status": "DECLARED",
        "vulnerability_status": "STATIC_POLICY_ONLY",
        "malicious_code_status": "PASSED",
        "findings": [],
    }
    assert result.report_hash == sha256(
        json.dumps(
            expected_report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert result.sbom_hash.startswith("sha256:")
    assert result.sbom["license"] == "Apache-2.0"
    assert result.signature_status == "NOT_PROVIDED"
    assert {path for path, _ in package.files} == {"SKILL.md", "manifest.yaml"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../SKILL.md", "/SKILL.md", "dir\\file", "a//b"])
async def test_import_rejects_unsafe_skill_paths(path: str) -> None:
    content, artifacts = package_content(extra_files={"safe.txt": b"safe"})
    content.files[-1].path = path

    with pytest.raises(ValueError, match="paths"):
        await validated(content, artifacts)


@pytest.mark.asyncio
async def test_import_rejects_casefold_duplicate_paths_and_hash_tampering() -> None:
    content, artifacts = package_content(extra_files={"skill.MD": b"duplicate"})
    with pytest.raises(ValueError, match="case folding"):
        await validated(content, artifacts)

    content, artifacts = package_content()
    manifest_artifact = next(
        artifact
        for declaration in content.files
        if declaration.path == "manifest.yaml"
        for artifact_id, artifact in artifacts.items()
        if str(artifact_id) == declaration.artifact_id
    )
    artifacts[manifest_artifact.artifact_id] = replace(
        manifest_artifact, content=manifest_artifact.content + b"\n# tampered"
    )
    with pytest.raises(ValueError, match="hash"):
        await validated(content, artifacts)


@pytest.mark.asyncio
async def test_import_rejects_manifest_drift_duplicate_keys_and_missing_references() -> (
    None
):
    payload = manifest_payload()
    drifted = deepcopy(payload)
    cast(dict[str, object], drifted["metadata"])["description"] = "Drifted"
    content, artifacts = package_content(
        payload, manifest_bytes=yaml.safe_dump(drifted, sort_keys=False).encode()
    )
    with pytest.raises(ValueError, match="does not match"):
        await validated(content, artifacts)

    duplicate_yaml = yaml.safe_dump(payload, sort_keys=False).replace(
        "kind: Skill", "kind: Skill\nkind: Skill"
    )
    content, artifacts = package_content(
        payload, manifest_bytes=duplicate_yaml.encode()
    )
    with pytest.raises(ValueError, match="valid safe YAML"):
        await validated(content, artifacts)

    payload = manifest_payload()
    payload["tests"] = [
        {
            "name": "missing",
            "inputFile": "tests/in.json",
            "expectedFile": "tests/out.json",
        }
    ]
    content, artifacts = package_content(payload)
    with pytest.raises(ValueError, match="inputFile"):
        await validated(content, artifacts)


@pytest.mark.asyncio
async def test_import_rejects_cross_owner_unavailable_expired_and_invalid_utf8() -> (
    None
):
    content, artifacts = package_content()
    artifact_id, artifact = next(iter(artifacts.items()))
    artifacts[artifact_id] = replace(artifact, owner_user_id=uuid4())
    with pytest.raises(ValueError, match="unavailable"):
        await validated(content, artifacts)

    content, artifacts = package_content()
    artifact_id, artifact = next(iter(artifacts.items()))
    artifacts[artifact_id] = replace(artifact, status="REJECTED")
    with pytest.raises(ValueError, match="AVAILABLE"):
        await validated(content, artifacts)

    content, artifacts = package_content()
    artifact_id, artifact = next(iter(artifacts.items()))
    artifacts[artifact_id] = replace(artifact, expires_at=NOW)
    with pytest.raises(ValueError, match="unexpired"):
        await validated(content, artifacts)

    content, artifacts = package_content()
    skill_declaration = next(item for item in content.files if item.path == "SKILL.md")
    artifact_id = UUID(skill_declaration.artifact_id)
    invalid = b"\xff"
    original = artifacts[artifact_id]
    artifacts[artifact_id] = SkillArtifactPayload(
        artifact_id=artifact_id,
        tenant_id=original.tenant_id,
        owner_user_id=original.owner_user_id,
        status=original.status,
        content_hash=sha256(invalid),
        size_bytes=len(invalid),
        content_type=original.content_type,
        expires_at=original.expires_at,
        content=invalid,
    )
    skill_declaration.content_hash = sha256(invalid)
    with pytest.raises(ValueError, match="UTF-8"):
        await validated(content, artifacts)


@pytest.mark.asyncio
async def test_scanner_rejects_unlocked_dependencies_unsafe_network_and_understated_risk() -> (
    None
):
    payload = manifest_payload()
    cast(dict[str, object], payload["dependencies"])["python"] = ["requests>=2"]
    permissions = cast(dict[str, object], payload["permissions"])
    network = cast(dict[str, object], permissions["network"])
    network["allowDomains"] = ["127.0.0.1"]
    permissions["secrets"] = ["secret-purpose:api-key"]
    content, artifacts = package_content(payload)

    result = await BaselineSkillSupplyChainScanner().scan(
        context(), await validated(content, artifacts)
    )

    assert result.status == "REJECTED"
    codes = {
        code
        for finding in result.findings
        if isinstance((code := finding.get("code")), str)
    }
    assert {
        "UNPINNED_PYTHON_DEPENDENCY",
        "IP_LITERAL_NETWORK_DESTINATION",
        "RUN_SANDBOX_REQUIRED",
        "RISK_LEVEL_UNDERSTATED",
    } <= codes


@pytest.mark.asyncio
async def test_hashed_requirements_lock_and_declared_dependency_pass() -> None:
    payload = manifest_payload()
    cast(dict[str, object], payload["dependencies"]).update(
        {"python": ["requests==2.32.5"], "lockFile": "requirements.lock"}
    )
    cast(dict[str, object], payload["sandbox"])["requiresRunSandbox"] = True
    lock = b"requests==2.32.5 --hash=sha256:" + b"b" * 64 + b"\n"
    content, artifacts = package_content(
        payload, extra_files={"requirements.lock": lock}
    )

    result = await BaselineSkillSupplyChainScanner().scan(
        context(), await validated(content, artifacts)
    )

    assert result.status == "PASSED"
    assert not result.findings
