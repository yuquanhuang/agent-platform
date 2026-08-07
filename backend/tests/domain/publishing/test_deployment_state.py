"""Deployment lifecycle and immutable identity tests."""

from uuid import UUID

import pytest

from packages.domain.public import (
    DeploymentTransitionError,
    deployment_compatibility_hash,
    deployment_id,
    ensure_deployment_transition,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RELEASE_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
SNAPSHOT_ID = UUID("44444444-4444-4444-8444-444444444444")
BUNDLE_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.mark.parametrize(
    ("current", "target"),
    (
        ("STAGED", "ACTIVE"),
        ("STAGED", "FAILED"),
        ("ACTIVE", "DEGRADED"),
        ("ACTIVE", "RETIRED"),
        ("DEGRADED", "ACTIVE"),
        ("DEGRADED", "RETIRED"),
    ),
)
def test_frozen_deployment_transitions_are_accepted(current: str, target: str) -> None:
    ensure_deployment_transition(current, target)  # type: ignore[arg-type]


def test_terminal_deployment_cannot_be_reactivated() -> None:
    with pytest.raises(DeploymentTransitionError):
        ensure_deployment_transition("RETIRED", "ACTIVE")


def test_deployment_identity_is_stable_per_release_and_target() -> None:
    first = deployment_id(TENANT_ID, RELEASE_ID, "rt_agentscope_a")

    assert first == deployment_id(TENANT_ID, RELEASE_ID, "rt_agentscope_a")
    assert first != deployment_id(TENANT_ID, RELEASE_ID, "rt_agentscope_b")


def test_compatibility_hash_covers_runtime_image_and_immutable_bundle() -> None:
    first = deployment_compatibility_hash(
        agent_id=AGENT_ID,
        snapshot_id=SNAPSHOT_ID,
        bundle_id=BUNDLE_ID,
        bundle_content_hash="sha256:" + "a" * 64,
        runtime_target_id="rt_agentscope_default",
        runtime_type="agentscope",
        runtime_image_digest="registry/agentscope@sha256:" + "b" * 64,
    )
    changed = deployment_compatibility_hash(
        agent_id=AGENT_ID,
        snapshot_id=SNAPSHOT_ID,
        bundle_id=BUNDLE_ID,
        bundle_content_hash="sha256:" + "a" * 64,
        runtime_target_id="rt_agentscope_default",
        runtime_type="agentscope",
        runtime_image_digest="registry/agentscope@sha256:" + "c" * 64,
    )

    assert first.startswith("sha256:")
    assert len(first) == 71
    assert first != changed
