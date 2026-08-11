"""SandboxPolicy secure-default, cross-field, and deterministic hash tests."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from packages.application.sandbox import (
    SandboxPolicyCompilationError,
    compile_sandbox_policy,
)

IMAGE_DIGEST = "runtime@sha256:" + "a" * 64
PolicyInput = dict[str, object]
PolicyMutator = Callable[[PolicyInput], None]


def policy_input() -> PolicyInput:
    return {
        "schema_version": "1.0",
        "scope": "run",
        "image_digest": IMAGE_DIGEST,
        "cpu_limit": 1.0,
        "memory_mb": 512,
        "disk_mb": 1024,
        "pids_limit": 64,
        "timeout_seconds": 600,
        "network": {
            "mode": "none",
            "allow_domains": [],
            "allow_ports": [],
            "deny_private_networks": True,
        },
        "filesystem": {
            "read_patterns": ["input/**", "work/**"],
            "write_patterns": ["output/**", "work/**"],
            "max_files": 1000,
            "max_file_bytes": 16 * 1024 * 1024,
        },
        "process": {
            "allowed_executables": ["python", "python3"],
            "shell_allowed": False,
            "max_processes": 32,
        },
        "artifacts": {
            "allow_export": True,
            "max_artifacts": 20,
            "max_total_bytes": 64 * 1024 * 1024,
            "allowed_content_types": ["application/json", "text/plain"],
        },
    }


def test_compile_applies_security_defaults_and_returns_immutable_snapshot() -> None:
    snapshot = compile_sandbox_policy(policy_input())

    policy = snapshot.load()
    assert policy.run_as_non_root is True
    assert policy.readonly_root_filesystem is True
    assert policy.network.max_redirects == 3
    assert policy.filesystem.allow_symlinks is False
    assert policy.filesystem.allow_device_files is False
    assert policy.process.termination_grace_seconds == 10
    assert policy.idle_ttl_seconds == 0
    assert snapshot.policy_hash.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        snapshot.policy_hash = "sha256:" + "0" * 64  # type: ignore[misc]


def test_compile_hash_is_order_independent_for_semantic_allowlists() -> None:
    first = policy_input()
    first["network"] = {
        "mode": "allowlist",
        "allow_domains": ["API.Example.COM.", "files.example.com"],
        "allow_ports": [443, 80],
        "deny_private_networks": True,
    }
    second = policy_input()
    second["network"] = {
        "deny_private_networks": True,
        "allow_ports": [80, 443],
        "allow_domains": ["files.example.com", "api.example.com"],
        "mode": "allowlist",
    }
    second["filesystem"] = {
        "write_patterns": ["work/**", "output/**"],
        "read_patterns": ["work/**", "input/**"],
        "max_file_bytes": 16 * 1024 * 1024,
        "max_files": 1000,
    }

    first_snapshot = compile_sandbox_policy(first)
    second_snapshot = compile_sandbox_policy(second)

    assert first_snapshot.policy_hash == second_snapshot.policy_hash
    assert first_snapshot.canonical_json == second_snapshot.canonical_json


def _network_none_with_allowlist(value: PolicyInput) -> None:
    value["network"] = {
        "mode": "none",
        "allow_domains": ["example.com"],
        "allow_ports": [443],
        "deny_private_networks": True,
    }


def _network_allowlist_without_ports(value: PolicyInput) -> None:
    value["network"] = {
        "mode": "allowlist",
        "allow_domains": ["example.com"],
        "allow_ports": [],
        "deny_private_networks": True,
    }


def _duplicate_effective_domains(value: PolicyInput) -> None:
    value["network"] = {
        "mode": "allowlist",
        "allow_domains": ["EXAMPLE.com", "example.com."],
        "allow_ports": [443],
        "deny_private_networks": True,
    }


def _duplicate_ports(value: PolicyInput) -> None:
    value["network"] = {
        "mode": "allowlist",
        "allow_domains": ["example.com"],
        "allow_ports": [443, 443],
        "deny_private_networks": True,
    }


def _processes_exceed_pids(value: PolicyInput) -> None:
    value["process"] = {
        "allowed_executables": ["python"],
        "shell_allowed": False,
        "max_processes": 65,
    }


def _file_exceeds_disk(value: PolicyInput) -> None:
    value["filesystem"] = {
        "read_patterns": [],
        "write_patterns": [],
        "max_files": 10,
        "max_file_bytes": 1025 * 1024 * 1024,
    }


def _run_idle_ttl(value: PolicyInput) -> None:
    value["idle_ttl_seconds"] = 30


UNSAFE_POLICIES: list[tuple[PolicyMutator, str]] = [
    (_network_none_with_allowlist, "mode none"),
    (_network_allowlist_without_ports, "requires both"),
    (_duplicate_effective_domains, "duplicate effective domains"),
    (_duplicate_ports, "duplicates"),
    (_processes_exceed_pids, "cannot exceed pids_limit"),
    (_file_exceeds_disk, "cannot exceed the sandbox disk limit"),
    (_run_idle_ttl, "idle_ttl_seconds=0"),
]


@pytest.mark.parametrize(("mutate", "message"), UNSAFE_POLICIES)
def test_compile_rejects_unsafe_cross_field_combinations(
    mutate: PolicyMutator, message: str
) -> None:
    value = policy_input()
    mutate(value)

    with pytest.raises(SandboxPolicyCompilationError, match=message):
        compile_sandbox_policy(value)


def test_compile_rejects_attempts_to_disable_schema_security_constants() -> None:
    value = policy_input()
    value["run_as_non_root"] = False

    with pytest.raises(ValidationError):
        compile_sandbox_policy(value)


def test_loaded_policy_mutation_does_not_change_snapshot() -> None:
    snapshot = compile_sandbox_policy(policy_input())
    loaded = snapshot.load()
    loaded.memory_mb = 2048

    assert snapshot.load().memory_mb == 512
    assert '"memory_mb":512' in snapshot.canonical_json
