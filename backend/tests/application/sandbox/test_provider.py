"""Sandbox Provider port safety and observation tests."""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from packages.application.sandbox import (
    ProviderProvisionSpec,
    ProviderSandboxObservation,
    SandboxProviderError,
    compile_sandbox_policy,
)


def _snapshot():
    return compile_sandbox_policy(
        {
            "schema_version": "1.0",
            "scope": "run",
            "image_digest": "runtime@sha256:" + "a" * 64,
            "cpu_limit": 1,
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
                "read_patterns": [],
                "write_patterns": [],
                "max_files": 100,
                "max_file_bytes": 1024,
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
        }
    )


def test_provider_provision_spec_is_frozen_and_contains_no_provision_secret() -> None:
    spec = ProviderProvisionSpec(
        sandbox_id="sbx_001",
        tenant_id="ten_001",
        run_id="run_001",
        image_digest="runtime@sha256:" + "a" * 64,
        bundle_ref="bundle://tenant/ten_001/snapshot/snp_001/runtime/agentscope/"
        + "sha256:"
        + "b" * 64,
        bundle_hash="sha256:" + "b" * 64,
        workspace_uri="workspace://tenant/ten_001/runs/run_001/",
        policy=_snapshot(),
    )

    assert "provision_token" not in {field.name for field in fields(spec)}
    assert "host_path" not in {field.name for field in fields(spec)}
    with pytest.raises(FrozenInstanceError):
        spec.sandbox_id = "changed"  # type: ignore[misc]


def test_provider_observation_rejects_non_utc_or_negative_usage() -> None:
    with pytest.raises(ValueError, match="UTC"):
        ProviderSandboxObservation(
            provider_ref="opaque-provider-ref",
            state="READY",
            observed_at=datetime(2026, 8, 9, tzinfo=timezone(timedelta(hours=8))),
        )
    with pytest.raises(ValueError, match="memory_bytes"):
        ProviderSandboxObservation(
            provider_ref="opaque-provider-ref",
            state="READY",
            observed_at=datetime.now(UTC),
            memory_bytes=-1,
        )


def test_provider_observation_accepts_equivalent_zero_offset_timezone() -> None:
    observation = ProviderSandboxObservation(
        provider_ref="opaque-provider-ref",
        state="READY",
        observed_at=datetime.now(UTC).astimezone(UTC),
        cpu_seconds=0,
        memory_bytes=0,
        disk_bytes=0,
        pids_current=0,
    )

    assert observation.observed_at.utcoffset() == timedelta(0)
    assert "docker_socket" not in {field.name for field in fields(observation)}


def test_provider_error_exposes_only_stable_safe_failure_fields() -> None:
    error = SandboxProviderError(
        code="PROVIDER_CAPACITY_EXHAUSTED",
        message="Sandbox capacity is temporarily unavailable.",
        retryable=True,
        provider_state="FAILED",
    )

    assert error.code == "PROVIDER_CAPACITY_EXHAUSTED"
    assert error.retryable is True
    assert error.provider_state == "FAILED"
    assert not hasattr(error, "raw_response")
    assert not hasattr(error, "credentials")
