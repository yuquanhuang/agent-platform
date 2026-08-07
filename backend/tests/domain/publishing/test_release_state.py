"""Release state machine tests."""

from typing import cast

import pytest

from packages.domain.public import (
    ReleaseStatus,
    ReleaseTransitionError,
    ensure_release_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("REQUESTED", "VALIDATING"),
        ("VALIDATING", "COMPILING"),
        ("COMPILING", "SCANNING"),
        ("SCANNING", "SMOKE_TESTING"),
        ("SCANNING", "ACTIVATING"),
        ("SMOKE_TESTING", "ACTIVATING"),
        ("ACTIVATING", "SUCCEEDED"),
    ],
)
def test_release_accepts_frozen_forward_transitions(current: str, target: str) -> None:
    ensure_release_transition(cast(ReleaseStatus, current), cast(ReleaseStatus, target))


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("REQUESTED", "SUCCEEDED"),
        ("SCANNING", "SUCCEEDED"),
        ("FAILED", "VALIDATING"),
        ("SUCCEEDED", "ACTIVATING"),
    ],
)
def test_release_rejects_skipped_or_terminal_transitions(
    current: str, target: str
) -> None:
    with pytest.raises(ReleaseTransitionError):
        ensure_release_transition(
            cast(ReleaseStatus, current), cast(ReleaseStatus, target)
        )
