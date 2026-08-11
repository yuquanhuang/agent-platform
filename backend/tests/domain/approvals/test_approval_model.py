"""Approval state-machine tests."""

import pytest

from packages.domain.approvals import ensure_approval_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("PENDING", "APPROVED"),
        ("PENDING", "REJECTED"),
        ("PENDING", "EXPIRED"),
        ("PENDING", "CANCELLED"),
        ("APPROVED", "CONSUMED"),
        ("APPROVED", "EXPIRED"),
    ],
)
def test_allows_frozen_approval_transitions(current: str, target: str) -> None:
    ensure_approval_transition(current, target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("PENDING", "CONSUMED"),
        ("APPROVED", "REJECTED"),
        ("REJECTED", "APPROVED"),
        ("EXPIRED", "PENDING"),
    ],
)
def test_rejects_invalid_approval_transitions(current: str, target: str) -> None:
    with pytest.raises(ValueError, match="Approval transition"):
        ensure_approval_transition(current, target)  # type: ignore[arg-type]
