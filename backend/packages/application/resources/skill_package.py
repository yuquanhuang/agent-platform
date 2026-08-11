"""Skill package validation and deterministic baseline supply-chain scanning."""

import hashlib
import ipaddress
import json
import re
import tomllib
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol, cast
from uuid import UUID

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from packages.application.metadata import RequestMetadata
from packages.contracts.generated.resource_content import (
    ResourceContentSkill,
    SkillManifest,
)
from packages.contracts.public import TenantContext
from packages.domain.public import (
    SkillArtifactPayload,
    SkillScanEvidenceRecord,
    SkillSupplyChainScanResult,
    ValidatedSkillPackage,
    canonical_content_hash,
)

_MAX_PACKAGE_BYTES = 100 * 1024 * 1024
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_INSTRUCTIONS_BYTES = 2 * 1024 * 1024
_PYTHON_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9_,.-]+\])?==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
_SYSTEM_PIN = re.compile(r"^[a-z0-9][a-z0-9+._-]*=[A-Za-z0-9][A-Za-z0-9.+:~_-]*$")
_REQUIREMENT_LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9_,.-]+\])?==(?P<version>[^\s;\\]+)"
)
_HASH_OPTION = re.compile(r"--hash=sha256:[a-f0-9]{64}(?:\s|$)")
_DANGEROUS_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "curl",
        "nc",
        "npm",
        "pip",
        "pip3",
        "powershell",
        "pwsh",
        "sh",
        "socat",
        "uv",
        "wget",
        "zsh",
    }
)


class SkillArtifactReader(Protocol):
    """Load trusted Artifact bytes already scoped to the tenant and owner."""

    async def load_artifacts(
        self,
        context: TenantContext,
        *,
        owner_user_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> dict[UUID, SkillArtifactPayload]: ...


class TrustedSkillArtifactContentReader(Protocol):
    """Read one canonical trusted object without exposing credentials or URLs."""

    async def read_trusted_artifact(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        object_uri: str,
        max_bytes: int,
    ) -> bytes | None: ...


class SkillSupplyChainScanner(Protocol):
    async def scan(
        self, context: TenantContext, package: ValidatedSkillPackage
    ) -> SkillSupplyChainScanResult: ...


class SkillScanStore(Protocol):
    async def record_scan(
        self,
        context: TenantContext,
        *,
        definition_id: UUID,
        draft_resource_version: int,
        content_hash: str,
        result: SkillSupplyChainScanResult,
        scanned_by: UUID,
        metadata: RequestMetadata,
    ) -> SkillScanEvidenceRecord: ...


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = _construct_yaml_object(loader, key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = _construct_yaml_object(loader, value_node, deep=deep)
    return mapping


def _construct_yaml_object(
    loader: _StrictSafeLoader, node: object, *, deep: bool
) -> object:
    return cast(
        object,
        loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
            node, deep=deep
        ),
    )


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


async def validate_skill_package(
    context: TenantContext,
    *,
    owner_user_id: UUID,
    content: ResourceContentSkill,
    artifact_reader: SkillArtifactReader,
    now: Callable[[], datetime] | None = None,
) -> ValidatedSkillPackage:
    """Resolve and validate the exact bytes referenced by a Skill draft."""

    clock = now or (lambda: datetime.now(UTC))
    normalized_files: list[tuple[str, UUID, str]] = []
    seen_paths: set[str] = set()
    artifact_hashes: dict[UUID, str] = {}
    for declared in content.files:
        path = safe_skill_path(declared.path)
        folded = path.casefold()
        if folded in seen_paths:
            raise ValueError("Skill file paths must be unique after case folding.")
        seen_paths.add(folded)
        try:
            artifact_id = UUID(declared.artifact_id)
        except ValueError as exc:
            raise ValueError("Skill artifact_id must be a UUID.") from exc
        prior_hash = artifact_hashes.setdefault(artifact_id, declared.content_hash)
        if prior_hash != declared.content_hash:
            raise ValueError(
                "One Skill Artifact ID cannot be declared with multiple hashes."
            )
        normalized_files.append((path, artifact_id, declared.content_hash))
    required = {"SKILL.md", "manifest.yaml"}
    if not required <= {path for path, _, _ in normalized_files}:
        raise ValueError("Skill package must contain root SKILL.md and manifest.yaml.")

    artifacts = await artifact_reader.load_artifacts(
        context,
        owner_user_id=owner_user_id,
        artifact_ids=tuple(sorted(artifact_hashes, key=str)),
    )
    resolved: list[tuple[str, SkillArtifactPayload]] = []
    total_size = 0
    expected_tenant = UUID(context.tenant_id)
    instant = clock()
    for path, artifact_id, declared_hash in normalized_files:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise ValueError("A Skill Artifact is unavailable to the current owner.")
        if (
            artifact.tenant_id != expected_tenant
            or artifact.owner_user_id != owner_user_id
        ):
            raise ValueError("A Skill Artifact is unavailable to the current owner.")
        if artifact.status != "AVAILABLE" or artifact.expires_at <= instant:
            raise ValueError("Skill Artifacts must be AVAILABLE and unexpired.")
        actual_hash = _hash_bytes(artifact.content)
        if (
            artifact.content_hash != declared_hash
            or actual_hash != declared_hash
            or artifact.size_bytes != len(artifact.content)
        ):
            raise ValueError("Skill Artifact metadata or content hash does not match.")
        total_size += artifact.size_bytes
        if total_size > _MAX_PACKAGE_BYTES:
            raise ValueError("Skill package exceeds the 100 MiB import limit.")
        resolved.append((path, artifact))

    files = dict(resolved)
    instructions = files["SKILL.md"].content
    if not instructions or len(instructions) > _MAX_INSTRUCTIONS_BYTES:
        raise ValueError("SKILL.md must be non-empty and at most 2 MiB.")
    _decode_utf8(instructions, "SKILL.md")
    manifest_bytes = files["manifest.yaml"].content
    if not manifest_bytes or len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest.yaml must be non-empty and at most 512 KiB.")
    parsed_manifest = _parse_manifest(manifest_bytes)
    if _manifest_json(parsed_manifest) != _manifest_json(content.manifest):
        raise ValueError("manifest.yaml does not match content.manifest.")
    _validate_manifest_file_references(content, set(files))
    return ValidatedSkillPackage(
        content=content,
        content_hash=canonical_content_hash(content),
        files=tuple(resolved),
    )


class BaselineSkillSupplyChainScanner:
    """Deterministic built-in policy scanner for frozen Skill package bytes."""

    name = "agent-platform-skill-baseline"
    version = "1.0.0"
    policy_version = "skill-supply-chain-v1"

    async def scan(
        self, context: TenantContext, package: ValidatedSkillPackage
    ) -> SkillSupplyChainScanResult:
        del context
        findings: list[dict[str, JsonValue]] = []
        manifest = package.content.manifest
        _check_json_schema("inputs", manifest.inputs, findings)
        _check_json_schema("outputs", manifest.outputs, findings)
        python_dependencies = _check_python_dependencies(manifest, findings)
        _check_system_dependencies(manifest, findings)
        file_bytes = {path: artifact.content for path, artifact in package.files}
        _check_lock_file(manifest, file_bytes, python_dependencies, findings)
        _check_network_permissions(manifest, findings)
        _check_command(manifest, findings)
        _check_risk_declaration(manifest, findings)
        sbom = _sbom(package, python_dependencies)
        sbom_hash = _hash_json(sbom)
        status = "REJECTED" if findings else "PASSED"
        report: dict[str, JsonValue] = {
            "scanner": self.name,
            "scanner_version": self.version,
            "policy_version": self.policy_version,
            "scan_profile": "STATIC_BASELINE",
            "status": status,
            "content_hash": package.content_hash,
            "sbom_hash": sbom_hash,
            "signature_status": "NOT_PROVIDED",
            "provenance_status": "NOT_PROVIDED",
            "license_status": (
                "DECLARED" if manifest.metadata.license else "NOT_PROVIDED"
            ),
            "vulnerability_status": "STATIC_POLICY_ONLY",
            "malicious_code_status": status,
            "findings": cast(list[JsonValue], findings),
        }
        return SkillSupplyChainScanResult(
            status=status,
            scanner_name=self.name,
            scanner_version=self.version,
            policy_version=self.policy_version,
            findings=tuple(findings),
            report_hash=_hash_json(report),
            sbom=sbom,
            sbom_hash=sbom_hash,
            signature_status="NOT_PROVIDED",
            provenance_status="NOT_PROVIDED",
        )


def safe_skill_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError("Skill paths must already use Unicode NFC normalization.")
    if not normalized or normalized.startswith("/") or "\\" in normalized:
        raise ValueError("Skill paths must be POSIX relative paths.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("Skill paths cannot contain control characters.")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Skill paths cannot contain empty or dot segments.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or str(path) != normalized:
        raise ValueError("Skill paths must be canonical POSIX relative paths.")
    return normalized


def _parse_manifest(content: bytes) -> SkillManifest:
    text = _decode_utf8(content, "manifest.yaml")
    try:
        tokens = cast(
            Iterable[object],
            yaml.scan(text),  # pyright: ignore[reportUnknownMemberType]
        )
        for token in tokens:
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise yaml.YAMLError(
                    "manifest.yaml cannot use anchors, aliases or tags."
                )
        documents = list(yaml.load_all(text, Loader=_StrictSafeLoader))
    except yaml.YAMLError as exc:
        raise ValueError("manifest.yaml is not valid safe YAML.") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("manifest.yaml must contain one mapping document.")
    try:
        return SkillManifest.model_validate(documents[0])
    except ValidationError as exc:
        raise ValueError("manifest.yaml does not satisfy Skill Manifest V1.") from exc


def _validate_manifest_file_references(
    content: ResourceContentSkill, paths: set[str]
) -> None:
    manifest = content.manifest
    lock_file = manifest.dependencies.lock_file
    if lock_file and safe_skill_path(lock_file) not in paths:
        raise ValueError("The declared dependency lockFile is missing.")
    for test in manifest.tests or []:
        if safe_skill_path(test.input_file) not in paths:
            raise ValueError("A Skill test inputFile is missing.")
        if safe_skill_path(test.expected_file) not in paths:
            raise ValueError("A Skill test expectedFile is missing.")
    for argument in manifest.entry.command or []:
        if argument.startswith("./") or "/" in argument:
            candidate = argument.removeprefix("./")
            if safe_skill_path(candidate) not in paths:
                raise ValueError("The Skill entry command references a missing file.")


def _check_json_schema(
    name: str, schema: JsonValue, findings: list[dict[str, JsonValue]]
) -> None:
    if not isinstance(schema, (bool, dict)):
        findings.append(_finding("INVALID_JSON_SCHEMA", f"/{name}"))
        return
    try:
        Draft202012Validator.check_schema(cast(bool | dict[str, object], schema))
    except SchemaError:
        findings.append(_finding("INVALID_JSON_SCHEMA", f"/{name}"))


def _check_python_dependencies(
    manifest: SkillManifest, findings: list[dict[str, JsonValue]]
) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for dependency in manifest.dependencies.python:
        if any(marker in dependency for marker in ("@", "http://", "https://", "git+")):
            findings.append(
                _finding("UNTRUSTED_PYTHON_DEPENDENCY", "/dependencies/python")
            )
            continue
        match = _PYTHON_PIN.fullmatch(dependency)
        if match is None:
            findings.append(
                _finding("UNPINNED_PYTHON_DEPENDENCY", "/dependencies/python")
            )
            continue
        dependencies[_normalized_package(match.group("name"))] = match.group("version")
    if dependencies and manifest.dependencies.lock_file is None:
        findings.append(_finding("PYTHON_LOCK_FILE_REQUIRED", "/dependencies/lockFile"))
    return dependencies


def _check_system_dependencies(
    manifest: SkillManifest, findings: list[dict[str, JsonValue]]
) -> None:
    for dependency in manifest.dependencies.system:
        if _SYSTEM_PIN.fullmatch(dependency) is None:
            findings.append(
                _finding("UNPINNED_SYSTEM_DEPENDENCY", "/dependencies/system")
            )


def _check_lock_file(
    manifest: SkillManifest,
    files: dict[str, bytes],
    expected: dict[str, str],
    findings: list[dict[str, JsonValue]],
) -> None:
    lock_file = manifest.dependencies.lock_file
    if not expected or lock_file is None:
        return
    content = files.get(lock_file)
    if not content:
        findings.append(_finding("LOCK_FILE_EMPTY", f"/files/{lock_file}"))
        return
    if lock_file == "requirements.lock":
        locked = _requirements_lock(content, findings)
    else:
        locked = _uv_lock(content, findings)
    for name, version in expected.items():
        if locked.get(name) != version:
            findings.append(
                _finding("LOCK_FILE_DEPENDENCY_MISMATCH", f"/files/{lock_file}")
            )


def _requirements_lock(
    content: bytes, findings: list[dict[str, JsonValue]]
) -> dict[str, str]:
    text = _decode_utf8(content, "requirements.lock").replace("\\\n", " ")
    locked: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or any(
            marker in line for marker in ("@", "http://", "https://", "git+")
        ):
            findings.append(
                _finding("UNTRUSTED_LOCK_ENTRY", "/files/requirements.lock")
            )
            continue
        match = _REQUIREMENT_LOCK_LINE.match(line)
        if match is None or _HASH_OPTION.search(f"{line} ") is None:
            findings.append(_finding("UNHASHED_LOCK_ENTRY", "/files/requirements.lock"))
            continue
        locked[_normalized_package(match.group("name"))] = match.group("version")
    return locked


def _uv_lock(content: bytes, findings: list[dict[str, JsonValue]]) -> dict[str, str]:
    try:
        payload = cast(
            dict[str, object], tomllib.loads(_decode_utf8(content, "uv.lock"))
        )
    except (tomllib.TOMLDecodeError, ValueError):
        findings.append(_finding("INVALID_UV_LOCK", "/files/uv.lock"))
        return {}
    packages = payload.get("package")
    if not isinstance(packages, list):
        findings.append(_finding("INVALID_UV_LOCK", "/files/uv.lock"))
        return {}
    locked: dict[str, str] = {}
    for package_value in cast(list[object], packages):
        if not isinstance(package_value, dict):
            continue
        package = cast(dict[str, object], package_value)
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        if isinstance(source, dict) and any(key in source for key in ("git", "url")):
            findings.append(_finding("UNTRUSTED_LOCK_ENTRY", "/files/uv.lock"))
            continue
        locked[_normalized_package(name)] = version
    return locked


def _check_network_permissions(
    manifest: SkillManifest, findings: list[dict[str, JsonValue]]
) -> None:
    for domain in manifest.permissions.network.allow_domains:
        normalized = domain.rstrip(".").lower()
        if (
            normalized != domain
            or not normalized
            or "*" in normalized
            or "://" in normalized
            or normalized == "localhost"
            or normalized.endswith((".localhost", ".local"))
        ):
            findings.append(
                _finding(
                    "UNSAFE_NETWORK_DESTINATION", "/permissions/network/allowDomains"
                )
            )
            continue
        try:
            ipaddress.ip_address(normalized.strip("[]"))
        except ValueError:
            continue
        findings.append(
            _finding(
                "IP_LITERAL_NETWORK_DESTINATION", "/permissions/network/allowDomains"
            )
        )


def _check_command(
    manifest: SkillManifest, findings: list[dict[str, JsonValue]]
) -> None:
    command = manifest.entry.command
    if not command:
        return
    executable = PurePosixPath(command[0]).name.casefold()
    if executable in _DANGEROUS_EXECUTABLES:
        findings.append(_finding("UNSAFE_ENTRY_EXECUTABLE", "/entry/command/0"))


def _check_risk_declaration(
    manifest: SkillManifest, findings: list[dict[str, JsonValue]]
) -> None:
    permissions = manifest.permissions
    has_execution = bool(
        manifest.entry.command
        or manifest.dependencies.python
        or manifest.dependencies.system
        or permissions.network.allow_domains
        or permissions.network.allow_ports
        or permissions.filesystem.write
        or permissions.tools
        or permissions.secrets
    )
    if has_execution and manifest.sandbox.requires_run_sandbox is not True:
        findings.append(_finding("RUN_SANDBOX_REQUIRED", "/sandbox/requiresRunSandbox"))
    required = "LOW"
    if permissions.network.allow_domains or permissions.tools or permissions.secrets:
        required = "MEDIUM"
    if manifest.entry.command or manifest.dependencies.system or permissions.secrets:
        required = "HIGH"
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    if order[manifest.sandbox.risk_level] < order[required]:
        findings.append(_finding("RISK_LEVEL_UNDERSTATED", "/sandbox/riskLevel"))


def _sbom(
    package: ValidatedSkillPackage, dependencies: dict[str, str]
) -> dict[str, JsonValue]:
    manifest = package.content.manifest
    files: list[JsonValue] = [
        {
            "path": path,
            "artifact_id": str(artifact.artifact_id),
            "content_hash": artifact.content_hash,
            "size_bytes": artifact.size_bytes,
        }
        for path, artifact in package.files
    ]
    components: list[JsonValue] = [
        {"type": "python", "name": name, "version": version}
        for name, version in sorted(dependencies.items())
    ]
    components.extend(
        {"type": "system", "name": dependency}
        for dependency in sorted(manifest.dependencies.system)
    )
    components.append({"type": "container", "digest": manifest.sandbox.image_digest})
    return {
        "format": "agent-platform-sbom-v1",
        "skill": manifest.metadata.name,
        "version": manifest.metadata.version,
        "license": manifest.metadata.license,
        "files": files,
        "components": components,
    }


def _manifest_json(manifest: SkillManifest) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _finding(code: str, path: str) -> dict[str, JsonValue]:
    return {"code": code, "severity": "HIGH", "path": path, "blocking": True}


def _normalized_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _decode_utf8(content: bytes, name: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8.") from exc


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _hash_json(value: object) -> str:
    content = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return _hash_bytes(content)
