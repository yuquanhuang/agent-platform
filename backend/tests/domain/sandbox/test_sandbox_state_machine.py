"""Sandbox lifecycle state transition tests."""

import pytest

from packages.domain.public import SANDBOX_STATUSES, ensure_sandbox_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("REQUESTED", "PROVISIONING"),
        ("PROVISIONING", "READY"),
        ("READY", "IN_USE"),
        ("IN_USE", "TERMINATING"),
        ("TERMINATING", "TERMINATED"),
        ("IN_USE", "QUARANTINED"),
        ("QUARANTINED", "TERMINATING"),
        ("FAILED", "TERMINATED"),
    ],
)
def test_expected_sandbox_transitions_are_allowed(current: str, target: str) -> None:
    ensure_sandbox_transition(current, target)  # type: ignore[arg-type]


def test_idempotent_transition_is_allowed() -> None:
    for status in SANDBOX_STATUSES:
        ensure_sandbox_transition(status, status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("REQUESTED", "READY"),
        ("READY", "TERMINATED"),
        ("IN_USE", "FAILED"),
        ("QUARANTINED", "READY"),
        ("TERMINATED", "READY"),
    ],
)
def test_illegal_or_terminal_reversal_is_rejected(current: str, target: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        ensure_sandbox_transition(current, target)  # type: ignore[arg-type]
