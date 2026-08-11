"""Canonical Workspace URI and quota rule tests."""

import pytest

from packages.domain.public import (
    WorkspaceUri,
    ensure_workspace_transition,
    ensure_workspace_usage,
)

ROOT = (
    "workspace://tenant/11111111-1111-4111-8111-111111111111/"
    "user/22222222-2222-4222-8222-222222222222/"
    "session/33333333-3333-4333-8333-333333333333/"
    "runs/44444444-4444-4444-8444-444444444444/"
)


def test_workspace_uri_round_trips_identity_and_child_path() -> None:
    root = WorkspaceUri.parse(ROOT)
    child = root.child("work", "résumé.txt")

    assert root.is_root()
    assert root.is_within(child)
    assert child.to_string() == ROOT + "work/résumé.txt/"
    assert WorkspaceUri.parse(child.to_string()) == child


@pytest.mark.parametrize(
    "value",
    [
        ROOT + "../escape/",
        ROOT + "work//file/",
        ROOT + "work/%2e%2e/file/",
        ROOT + "work\\file/",
        ROOT + "work/file?download=1",
        ROOT + "work/e\u0301.txt/",
        ROOT.rstrip("/"),
    ],
)
def test_workspace_uri_rejects_ambiguous_or_escaping_paths(value: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceUri.parse(value)


def test_workspace_identity_prevents_cross_run_prefix_confusion() -> None:
    root = WorkspaceUri.parse(ROOT)
    other = WorkspaceUri.root(
        tenant_id=root.tenant_id,
        user_id=root.user_id,
        session_id=root.session_id,
        run_id="55555555-5555-4555-8555-555555555555",
    ).child("work")

    assert not root.is_within(other)


def test_workspace_usage_and_lifecycle_are_bounded() -> None:
    ensure_workspace_usage(quota_bytes=1024, used_bytes=1024, max_files=2, file_count=2)
    ensure_workspace_transition("ACTIVE", "SEALED")
    ensure_workspace_transition("SEALED", "QUARANTINED")

    with pytest.raises(ValueError, match="byte usage"):
        ensure_workspace_usage(
            quota_bytes=1024, used_bytes=1025, max_files=2, file_count=1
        )
    with pytest.raises(ValueError, match="not allowed"):
        ensure_workspace_transition("SEALED", "ACTIVE")
