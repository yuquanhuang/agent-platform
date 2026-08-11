"""Deterministic effective-policy intersection for immutable Agent graphs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from packages.contracts.generated.resource_content import (
    SandboxPolicy,
    SkillManifest,
)
from packages.domain.mcp import McpDiscoveredTool

EFFECTIVE_POLICY_SCHEMA_VERSION = "effective-policy/v1"
POLICY_VERSION = "agent-platform-policy/1"
PolicyDecision = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
PolicyResourceKind = Literal["sandbox", "skill", "mcp"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]

_RISK_ORDER: dict[str, int] = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "CRITICAL": 3,
}


class PolicyCompilationError(ValueError):
    """Stable fail-closed error produced while intersecting policy inputs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PolicyResourceSource:
    kind: PolicyResourceKind
    resource_id: UUID
    version_id: UUID
    content_hash: str


@dataclass(frozen=True, slots=True)
class SkillPolicySource:
    source: PolicyResourceSource
    manifest: SkillManifest


@dataclass(frozen=True, slots=True)
class McpPolicySource:
    source: PolicyResourceSource
    tools: tuple[McpDiscoveredTool, ...]


@dataclass(frozen=True, slots=True)
class EffectivePolicySnapshot:
    """Canonical immutable policy evidence embedded into one Runtime Bundle."""

    schema_version: str
    policy_version: str
    decision: PolicyDecision
    reason_codes: tuple[str, ...]
    sandbox_policy: SandboxPolicy
    maximum_risk_level: RiskLevel
    canonical_json: str
    policy_hash: str


def compile_effective_policy(
    *,
    sandbox_policies: Iterable[tuple[PolicyResourceSource, SandboxPolicy]],
    skills: Iterable[SkillPolicySource] = (),
    mcps: Iterable[McpPolicySource] = (),
) -> EffectivePolicySnapshot:
    """Intersect immutable policy sources and return content-addressed evidence."""

    sandbox_values = tuple(sandbox_policies)
    skill_values = tuple(skills)
    mcp_values = tuple(mcps)
    if not sandbox_values:
        raise PolicyCompilationError(
            "SANDBOX_POLICY_REQUIRED",
            "An effective policy requires at least one SandboxPolicy source.",
        )

    effective_sandbox = _intersect_sandbox_policies(
        tuple(policy for _, policy in sandbox_values),
        skills=skill_values,
    )
    tools = _authorized_tools(mcp_values)
    _validate_skill_permissions(skill_values, effective_sandbox, tools)
    maximum_risk = _maximum_risk(skill_values, mcp_values)
    decision, reason_codes = _decision(maximum_risk)
    sources = sorted(
        (
            *(source for source, _ in sandbox_values),
            *(skill.source for skill in skill_values),
            *(mcp.source for mcp in mcp_values),
        ),
        key=lambda item: (item.kind, str(item.resource_id), str(item.version_id)),
    )
    permissions = _effective_permissions(skill_values, tools)
    payload: dict[str, object] = {
        "schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "decision": decision,
        "reason_codes": list(reason_codes),
        "maximum_risk_level": maximum_risk,
        "effective_constraints": {
            "sandbox": effective_sandbox.model_dump(mode="json"),
            "permissions": permissions,
        },
        "sources": [
            {
                "kind": source.kind,
                "resource_id": str(source.resource_id),
                "version_id": str(source.version_id),
                "content_hash": source.content_hash,
            }
            for source in sources
        ],
    }
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return EffectivePolicySnapshot(
        schema_version=EFFECTIVE_POLICY_SCHEMA_VERSION,
        policy_version=POLICY_VERSION,
        decision=decision,
        reason_codes=reason_codes,
        sandbox_policy=effective_sandbox,
        maximum_risk_level=maximum_risk,
        canonical_json=canonical_json,
        policy_hash=f"sha256:{digest}",
    )


def _intersect_sandbox_policies(
    policies: tuple[SandboxPolicy, ...],
    *,
    skills: tuple[SkillPolicySource, ...],
) -> SandboxPolicy:
    images = {policy.image_digest for policy in policies}
    images.update(skill.manifest.sandbox.image_digest for skill in skills)
    if len(images) != 1:
        raise PolicyCompilationError(
            "POLICY_IMAGE_CONFLICT",
            "Sandbox and Skill image digests do not have one safe intersection.",
        )

    force_run = any(
        skill.manifest.sandbox.requires_run_sandbox is not False for skill in skills
    )
    scope: Literal["run", "session"] = (
        "run"
        if force_run or any(policy.scope == "run" for policy in policies)
        else "session"
    )
    network_none = any(policy.network.mode == "none" for policy in policies)
    if network_none:
        network_mode: Literal["none", "allowlist"] = "none"
        domains: list[str] = []
        ports: list[int] = []
    else:
        network_mode = "allowlist"
        domains = sorted(
            _string_intersection(
                tuple(
                    _normalized_domains(policy.network.allow_domains)
                    for policy in policies
                )
            )
        )
        ports = sorted(
            _integer_intersection(
                tuple(policy.network.allow_ports for policy in policies)
            )
        )
        if not domains or not ports:
            raise PolicyCompilationError(
                "POLICY_NETWORK_INTERSECTION_EMPTY",
                "Allowlist Sandbox policies have no usable domain and port intersection.",
            )

    timeout_candidates = [policy.timeout_seconds for policy in policies]
    timeout_candidates.extend(
        skill.manifest.sandbox.timeout_seconds for skill in skills
    )
    effective = SandboxPolicy.model_validate(
        {
            "schema_version": "1.0",
            "scope": scope,
            "image_digest": images.pop(),
            "cpu_limit": min(policy.cpu_limit for policy in policies),
            "memory_mb": min(policy.memory_mb for policy in policies),
            "disk_mb": min(policy.disk_mb for policy in policies),
            "pids_limit": min(policy.pids_limit for policy in policies),
            "timeout_seconds": min(timeout_candidates),
            "run_as_non_root": True,
            "readonly_root_filesystem": True,
            "network": {
                "mode": network_mode,
                "allow_domains": domains,
                "allow_ports": ports,
                "deny_private_networks": True,
                "max_redirects": min(
                    (
                        policy.network.max_redirects
                        if policy.network.max_redirects is not None
                        else 3
                    )
                    for policy in policies
                ),
            },
            "filesystem": {
                "read_patterns": sorted(
                    _string_intersection(
                        tuple(policy.filesystem.read_patterns for policy in policies)
                    )
                ),
                "write_patterns": sorted(
                    _string_intersection(
                        tuple(policy.filesystem.write_patterns for policy in policies)
                    )
                ),
                "max_files": min(policy.filesystem.max_files for policy in policies),
                "max_file_bytes": min(
                    policy.filesystem.max_file_bytes for policy in policies
                ),
                "allow_symlinks": False,
                "allow_device_files": False,
            },
            "process": {
                "allowed_executables": sorted(
                    _string_intersection(
                        tuple(policy.process.allowed_executables for policy in policies)
                    )
                ),
                "shell_allowed": all(
                    policy.process.shell_allowed for policy in policies
                ),
                "max_processes": min(
                    policy.process.max_processes for policy in policies
                ),
                "termination_grace_seconds": min(
                    (
                        policy.process.termination_grace_seconds
                        if policy.process.termination_grace_seconds is not None
                        else 10
                    )
                    for policy in policies
                ),
            },
            "artifacts": {
                "allow_export": all(
                    policy.artifacts.allow_export for policy in policies
                ),
                "max_artifacts": min(
                    policy.artifacts.max_artifacts for policy in policies
                ),
                "max_total_bytes": min(
                    policy.artifacts.max_total_bytes for policy in policies
                ),
                "allowed_content_types": sorted(
                    _string_intersection(
                        tuple(
                            policy.artifacts.allowed_content_types
                            for policy in policies
                        )
                    )
                ),
            },
            "idle_ttl_seconds": (
                0
                if scope == "run"
                else min(
                    (
                        policy.idle_ttl_seconds
                        if policy.idle_ttl_seconds is not None
                        else 0
                    )
                    for policy in policies
                )
            ),
            "max_lifetime_seconds": min(
                (
                    policy.max_lifetime_seconds
                    if policy.max_lifetime_seconds is not None
                    else 86400
                )
                for policy in policies
            ),
        }
    )
    _validate_cross_fields(effective)
    return effective


def _validate_skill_permissions(
    skills: tuple[SkillPolicySource, ...],
    sandbox: SandboxPolicy,
    tools: dict[str, dict[str, object]],
) -> None:
    executable_tools = set(sandbox.process.allowed_executables)
    authorized_tools = executable_tools | set(tools)
    for skill in skills:
        permissions = skill.manifest.permissions
        if not set(permissions.filesystem.read) <= set(
            sandbox.filesystem.read_patterns
        ):
            raise PolicyCompilationError(
                "POLICY_FILESYSTEM_READ_DENIED",
                "A Skill requests filesystem read access outside the effective policy.",
            )
        if not set(permissions.filesystem.write) <= set(
            sandbox.filesystem.write_patterns
        ):
            raise PolicyCompilationError(
                "POLICY_FILESYSTEM_WRITE_DENIED",
                "A Skill requests filesystem write access outside the effective policy.",
            )
        if not set(_normalized_domains(permissions.network.allow_domains)) <= set(
            sandbox.network.allow_domains
        ):
            raise PolicyCompilationError(
                "POLICY_NETWORK_DOMAIN_DENIED",
                "A Skill requests network domains outside the effective policy.",
            )
        if not set(permissions.network.allow_ports) <= set(sandbox.network.allow_ports):
            raise PolicyCompilationError(
                "POLICY_NETWORK_PORT_DENIED",
                "A Skill requests network ports outside the effective policy.",
            )
        if not set(permissions.tools) <= authorized_tools:
            raise PolicyCompilationError(
                "POLICY_TOOL_NOT_AUTHORIZED",
                "A Skill requests a tool not frozen by MCP or Sandbox policy.",
            )
        command = skill.manifest.entry.command
        if command and command[0] not in executable_tools:
            raise PolicyCompilationError(
                "POLICY_EXECUTABLE_DENIED",
                "A Skill entry command is not allowed by the effective Sandbox policy.",
            )


def _authorized_tools(
    mcps: tuple[McpPolicySource, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for mcp in mcps:
        for tool in mcp.tools:
            existing = result.get(tool.name)
            value: dict[str, object] = {
                "name": tool.name,
                "schema_hash": tool.schema_hash,
                "risk_level": tool.risk_level,
                "resource_id": str(mcp.source.resource_id),
                "version_id": str(mcp.source.version_id),
            }
            if existing is not None and existing != value:
                raise PolicyCompilationError(
                    "POLICY_TOOL_IDENTITY_CONFLICT",
                    "One tool name resolves to conflicting immutable MCP capabilities.",
                )
            result[tool.name] = value
    return result


def _effective_permissions(
    skills: tuple[SkillPolicySource, ...],
    tools: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        "filesystem": {
            "read": sorted(
                {
                    value
                    for skill in skills
                    for value in skill.manifest.permissions.filesystem.read
                }
            ),
            "write": sorted(
                {
                    value
                    for skill in skills
                    for value in skill.manifest.permissions.filesystem.write
                }
            ),
        },
        "network": {
            "allow_domains": sorted(
                {
                    value
                    for skill in skills
                    for value in _normalized_domains(
                        skill.manifest.permissions.network.allow_domains
                    )
                }
            ),
            "allow_ports": sorted(
                {
                    value
                    for skill in skills
                    for value in skill.manifest.permissions.network.allow_ports
                }
            ),
        },
        "tools": [tools[name] for name in sorted(tools)],
        "secrets": sorted(
            {value for skill in skills for value in skill.manifest.permissions.secrets}
        ),
    }


def _maximum_risk(
    skills: tuple[SkillPolicySource, ...],
    mcps: tuple[McpPolicySource, ...],
) -> RiskLevel:
    values: list[RiskLevel] = [skill.manifest.sandbox.risk_level for skill in skills]
    values.extend(tool.risk_level for mcp in mcps for tool in mcp.tools)
    return max(values or ["LOW"], key=lambda value: _RISK_ORDER[value])


def _decision(risk: RiskLevel) -> tuple[PolicyDecision, tuple[str, ...]]:
    if risk == "CRITICAL":
        return "DENY", ("CRITICAL_TOOL_DENIED",)
    if risk == "HIGH":
        return "REQUIRE_APPROVAL", ("HIGH_RISK_TOOL_REQUIRES_APPROVAL",)
    return "ALLOW", ()


def _validate_cross_fields(policy: SandboxPolicy) -> None:
    if policy.process.max_processes > policy.pids_limit:
        raise PolicyCompilationError(
            "POLICY_PROCESS_LIMIT_CONFLICT",
            "Effective process.max_processes exceeds the PID limit.",
        )
    if policy.filesystem.max_file_bytes > policy.disk_mb * 1024 * 1024:
        raise PolicyCompilationError(
            "POLICY_FILE_LIMIT_CONFLICT",
            "Effective max_file_bytes exceeds the disk limit.",
        )


def _normalized_domains(values: Iterable[str]) -> list[str]:
    return [value.rstrip(".").lower() for value in values]


def _string_intersection(values: tuple[list[str], ...]) -> set[str]:
    if not values:
        return set()
    result = set(values[0])
    for value in values[1:]:
        result.intersection_update(value)
    return result


def _integer_intersection(values: tuple[list[int], ...]) -> set[int]:
    if not values:
        return set()
    result = set(values[0])
    for value in values[1:]:
        result.intersection_update(value)
    return result
