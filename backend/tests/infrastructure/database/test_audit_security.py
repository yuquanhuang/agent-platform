"""Audit write-time minimization tests."""

import hashlib
from uuid import UUID

from packages.infrastructure.database.audit_security import (
    audit_change_digest,
    infer_audit_run_id,
    sanitize_audit_metadata,
)


def test_audit_metadata_redacts_sensitive_values_and_bounds_content() -> None:
    metadata = sanitize_audit_metadata(
        {
            "secret_ref": "secret://tenant/prod/key",
            "ticket_nonce": "plain-ticket-nonce",
            "tool_schema_hash": "sha256:" + "a" * 64,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "large": "x" * 600,
        }
    )
    assert metadata["secret_ref"] == "[REDACTED]"
    assert metadata["ticket_nonce"] == "[REDACTED]"
    assert metadata["tool_schema_hash"] == "sha256:" + "a" * 64
    large = metadata["large"]
    assert isinstance(large, dict)
    assert large == {
        "sha256": hashlib.sha256(("x" * 600).encode()).hexdigest(),
        "length": 600,
        "truncated": True,
    }
    assert audit_change_digest(metadata).startswith("sha256:")


def test_audit_run_id_is_inferred_from_run_resource_or_metadata() -> None:
    run_id = UUID("11111111-1111-4111-8111-111111111111")
    assert (
        infer_audit_run_id(resource_type="run", resource_id=run_id, metadata={})
        == run_id
    )
    assert (
        infer_audit_run_id(
            resource_type="tool", resource_id=None, metadata={"run_id": str(run_id)}
        )
        == run_id
    )
