"""Compile mutable SandboxPolicy inputs into deterministic immutable snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from packages.contracts.generated.resource_content import SandboxPolicy

_MEBIBYTE = 1024 * 1024


class SandboxPolicyCompilationError(ValueError):
    """A policy is valid JSON Schema input but violates platform safety rules."""


@dataclass(frozen=True, slots=True)
class FrozenSandboxPolicy:
    """Immutable canonical representation used by persistence and Provider ports."""

    schema_version: str
    canonical_json: str
    policy_hash: str

    def load(self) -> SandboxPolicy:
        """Return a fresh Pydantic model so callers cannot mutate the snapshot."""

        return SandboxPolicy.model_validate_json(self.canonical_json)


def compile_sandbox_policy(
    source: SandboxPolicy | Mapping[str, object],
) -> FrozenSandboxPolicy:
    """Apply secure defaults, validate cross-field rules, and hash canonical JSON."""

    if isinstance(source, SandboxPolicy):
        payload = source.model_dump(mode="json", exclude_none=True)
    else:
        payload = dict(source)
    normalized = _apply_secure_defaults(payload)
    policy = SandboxPolicy.model_validate(normalized)
    canonical_payload = policy.model_dump(mode="json")
    _normalize_unordered_fields(canonical_payload)
    normalized_policy = SandboxPolicy.model_validate(canonical_payload)
    _validate_effective_policy(normalized_policy)
    canonical_json = json.dumps(
        normalized_policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return FrozenSandboxPolicy(
        schema_version=normalized_policy.schema_version,
        canonical_json=canonical_json,
        policy_hash=f"sha256:{digest}",
    )


def _apply_secure_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("run_as_non_root", True)
    normalized.setdefault("readonly_root_filesystem", True)
    normalized.setdefault("idle_ttl_seconds", 0)
    normalized.setdefault("max_lifetime_seconds", 86400)
    network = _nested_mapping(normalized, "network")
    network.setdefault("max_redirects", 3)
    normalized["network"] = network
    filesystem = _nested_mapping(normalized, "filesystem")
    filesystem.setdefault("allow_symlinks", False)
    filesystem.setdefault("allow_device_files", False)
    normalized["filesystem"] = filesystem
    process = _nested_mapping(normalized, "process")
    process.setdefault("termination_grace_seconds", 10)
    normalized["process"] = process
    return normalized


def _nested_mapping(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise SandboxPolicyCompilationError(f"{field_name} must be an object")
    return dict(cast(Mapping[str, Any], value))


def _normalize_unordered_fields(payload: dict[str, Any]) -> None:
    network = cast(dict[str, Any], payload["network"])
    domains = cast(list[str], network["allow_domains"])
    normalized_domains = [_normalize_domain(domain) for domain in domains]
    if len(set(normalized_domains)) != len(normalized_domains):
        raise SandboxPolicyCompilationError(
            "network.allow_domains contains duplicate effective domains"
        )
    ports = cast(list[int], network["allow_ports"])
    if len(set(ports)) != len(ports):
        raise SandboxPolicyCompilationError("network.allow_ports contains duplicates")
    network["allow_domains"] = sorted(normalized_domains)
    network["allow_ports"] = sorted(ports)

    filesystem = cast(dict[str, Any], payload["filesystem"])
    filesystem["read_patterns"] = sorted(
        set(cast(list[str], filesystem["read_patterns"]))
    )
    filesystem["write_patterns"] = sorted(
        set(cast(list[str], filesystem["write_patterns"]))
    )
    process = cast(dict[str, Any], payload["process"])
    process["allowed_executables"] = sorted(
        set(cast(list[str], process["allowed_executables"]))
    )
    artifacts = cast(dict[str, Any], payload["artifacts"])
    artifacts["allowed_content_types"] = sorted(
        set(cast(list[str], artifacts["allowed_content_types"]))
    )


def _normalize_domain(domain: str) -> str:
    if domain != domain.strip():
        raise SandboxPolicyCompilationError(
            "network.allow_domains cannot contain surrounding whitespace"
        )
    normalized = domain.rstrip(".").lower()
    if not normalized:
        raise SandboxPolicyCompilationError(
            "network.allow_domains cannot contain an empty effective domain"
        )
    return normalized


def _validate_effective_policy(policy: SandboxPolicy) -> None:
    if policy.network.mode == "none":
        if policy.network.allow_domains or policy.network.allow_ports:
            raise SandboxPolicyCompilationError(
                "network mode none cannot define domain or port allowlists"
            )
    elif not policy.network.allow_domains or not policy.network.allow_ports:
        raise SandboxPolicyCompilationError(
            "network mode allowlist requires both domains and ports"
        )
    if policy.process.max_processes > policy.pids_limit:
        raise SandboxPolicyCompilationError(
            "process.max_processes cannot exceed pids_limit"
        )
    if policy.filesystem.max_file_bytes > policy.disk_mb * _MEBIBYTE:
        raise SandboxPolicyCompilationError(
            "filesystem.max_file_bytes cannot exceed the sandbox disk limit"
        )
    if policy.scope == "run" and policy.idle_ttl_seconds != 0:
        raise SandboxPolicyCompilationError(
            "run-scoped sandboxes must have idle_ttl_seconds=0"
        )
