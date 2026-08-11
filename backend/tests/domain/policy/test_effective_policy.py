"""Effective-policy intersection, risk, and immutable snapshot tests."""

from uuid import UUID

import pytest

from packages.contracts.generated.resource_content import SandboxPolicy, SkillManifest
from packages.domain.mcp import McpDiscoveredTool
from packages.domain.policy import (
    McpPolicySource,
    PolicyCompilationError,
    PolicyResourceSource,
    SkillPolicySource,
    compile_effective_policy,
)

HASH = "sha256:" + "a" * 64
IMAGE = "registry.example/runtime@sha256:" + "b" * 64


def source(kind: str, value: int) -> PolicyResourceSource:
    return PolicyResourceSource(
        kind=kind,  # type: ignore[arg-type]
        resource_id=UUID(f"00000000-0000-4000-8000-{value:012d}"),
        version_id=UUID(f"10000000-0000-4000-8000-{value:012d}"),
        content_hash=HASH,
    )


def sandbox(
    *,
    scope: str,
    cpu: float,
    domains: list[str],
    ports: list[int],
    reads: list[str],
    executables: list[str],
) -> SandboxPolicy:
    return SandboxPolicy.model_validate(
        {
            "schema_version": "1.0",
            "scope": scope,
            "image_digest": IMAGE,
            "cpu_limit": cpu,
            "memory_mb": 1024,
            "disk_mb": 2048,
            "pids_limit": 64,
            "timeout_seconds": 600,
            "network": {
                "mode": "allowlist",
                "allow_domains": domains,
                "allow_ports": ports,
                "deny_private_networks": True,
            },
            "filesystem": {
                "read_patterns": reads,
                "write_patterns": ["workspace/**"],
                "max_files": 1000,
                "max_file_bytes": 1048576,
            },
            "process": {
                "allowed_executables": executables,
                "shell_allowed": False,
                "max_processes": 32,
            },
            "artifacts": {
                "allow_export": True,
                "max_artifacts": 20,
                "max_total_bytes": 10485760,
                "allowed_content_types": ["text/plain", "application/json"],
            },
            "idle_ttl_seconds": 300,
            "max_lifetime_seconds": 3600,
        }
    )


def skill(*, domains: list[str], risk: str = "LOW") -> SkillManifest:
    return SkillManifest.model_validate(
        {
            "apiVersion": "agent-platform/v1",
            "kind": "Skill",
            "metadata": {
                "name": "policy-test",
                "version": "1.0.0",
                "displayName": "Policy Test",
                "description": "Policy test skill.",
            },
            "runtime": {
                "compatible": ["agentscope"],
                "minimumPlatformVersion": "1.0.0",
            },
            "entry": {"instructions": "SKILL.md"},
            "permissions": {
                "filesystem": {"read": ["shared/**"], "write": ["workspace/**"]},
                "network": {"allowDomains": domains, "allowPorts": [443]},
                "tools": ["python"],
                "secrets": [],
            },
            "dependencies": {"python": [], "system": []},
            "inputs": {"type": "object"},
            "outputs": {"type": "object"},
            "sandbox": {
                "imageDigest": IMAGE,
                "timeoutSeconds": 300,
                "riskLevel": risk,
                "requiresRunSandbox": True,
            },
        }
    )


def test_effective_policy_is_deterministic_and_takes_strict_intersection() -> None:
    broad = sandbox(
        scope="session",
        cpu=4,
        domains=["api.example", "shared.example"],
        ports=[80, 443],
        reads=["input/**", "shared/**"],
        executables=["bash", "python"],
    )
    narrow = sandbox(
        scope="run",
        cpu=2,
        domains=["shared.example"],
        ports=[443],
        reads=["shared/**"],
        executables=["python"],
    )
    first = compile_effective_policy(
        sandbox_policies=(
            (source("sandbox", 1), broad),
            (source("sandbox", 2), narrow),
        ),
        skills=(
            SkillPolicySource(source("skill", 3), skill(domains=["shared.example"])),
        ),
    )
    second = compile_effective_policy(
        sandbox_policies=(
            (source("sandbox", 2), narrow),
            (source("sandbox", 1), broad),
        ),
        skills=(
            SkillPolicySource(source("skill", 3), skill(domains=["shared.example"])),
        ),
    )

    assert first == second
    assert first.decision == "ALLOW"
    assert first.sandbox_policy.scope == "run"
    assert first.sandbox_policy.cpu_limit == 2
    assert first.sandbox_policy.network.allow_domains == ["shared.example"]
    assert first.sandbox_policy.network.allow_ports == [443]
    assert first.sandbox_policy.filesystem.read_patterns == ["shared/**"]
    assert first.sandbox_policy.process.allowed_executables == ["python"]
    assert first.policy_hash.startswith("sha256:")


def test_skill_cannot_expand_effective_network_policy() -> None:
    policy = sandbox(
        scope="run",
        cpu=1,
        domains=["allowed.example"],
        ports=[443],
        reads=["shared/**"],
        executables=["python"],
    )

    with pytest.raises(PolicyCompilationError) as error:
        compile_effective_policy(
            sandbox_policies=((source("sandbox", 1), policy),),
            skills=(
                SkillPolicySource(
                    source("skill", 2), skill(domains=["denied.example"])
                ),
            ),
        )

    assert error.value.code == "POLICY_NETWORK_DOMAIN_DENIED"


def test_high_risk_tool_requires_approval_and_critical_tool_is_denied() -> None:
    policy = sandbox(
        scope="run",
        cpu=1,
        domains=["allowed.example"],
        ports=[443],
        reads=["shared/**"],
        executables=["python"],
    )

    def snapshot(risk: str):
        return compile_effective_policy(
            sandbox_policies=((source("sandbox", 1), policy),),
            mcps=(
                McpPolicySource(
                    source("mcp", 2),
                    (
                        McpDiscoveredTool(
                            name="ops.execute",
                            description=None,
                            input_schema={"type": "object"},
                            output_schema=None,
                            schema_hash=HASH,
                            risk_level=risk,  # type: ignore[arg-type]
                        ),
                    ),
                ),
            ),
        )

    high = snapshot("HIGH")
    critical = snapshot("CRITICAL")

    assert high.decision == "REQUIRE_APPROVAL"
    assert high.reason_codes == ("HIGH_RISK_TOOL_REQUIRES_APPROVAL",)
    assert critical.decision == "DENY"
    assert critical.reason_codes == ("CRITICAL_TOOL_DENIED",)
