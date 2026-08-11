"""Artifact lifecycle and trusted URI domain tests."""

from uuid import UUID

import pytest

from packages.domain.public import artifact_uri, ensure_artifact_transition


def test_artifact_upload_and_scan_transitions_are_explicit() -> None:
    ensure_artifact_transition("UPLOADING", "SCANNING")
    ensure_artifact_transition("SCANNING", "AVAILABLE")
    ensure_artifact_transition("SCANNING", "REJECTED")
    ensure_artifact_transition("SCANNING", "FAILED")


def test_artifact_cannot_become_available_without_scanning() -> None:
    with pytest.raises(ValueError, match="UPLOADING -> AVAILABLE"):
        ensure_artifact_transition("UPLOADING", "AVAILABLE")


def test_artifact_trusted_uri_is_tenant_and_identity_bound() -> None:
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    artifact_id = UUID("22222222-2222-4222-8222-222222222222")

    assert artifact_uri(tenant_id=tenant_id, artifact_id=artifact_id) == (
        "artifact://tenant/11111111-1111-4111-8111-111111111111/"
        "artifact/22222222-2222-4222-8222-222222222222"
    )
