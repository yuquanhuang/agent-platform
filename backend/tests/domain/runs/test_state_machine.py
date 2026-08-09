"""Run and RunAttempt lifecycle transitions are explicit and terminal."""

import pytest

from packages.domain.public import (
    ensure_run_attempt_transition,
    ensure_run_transition,
)


def test_run_allows_expected_execution_path() -> None:
    ensure_run_transition("CREATED", "QUEUED")
    ensure_run_transition("QUEUED", "PREPARING")
    ensure_run_transition("PREPARING", "RUNNING")
    ensure_run_transition("RUNNING", "SUCCEEDED")


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"])
def test_run_terminal_states_reject_further_transition(terminal: str) -> None:
    with pytest.raises(ValueError):
        ensure_run_transition(terminal, "RUNNING")  # type: ignore[arg-type]


def test_run_attempt_allows_fenced_worker_path_and_rejects_reopen() -> None:
    ensure_run_attempt_transition("ALLOCATED", "STARTING")
    ensure_run_attempt_transition("STARTING", "RUNNING")
    ensure_run_attempt_transition("RUNNING", "COMPLETED")
    with pytest.raises(ValueError):
        ensure_run_attempt_transition("COMPLETED", "RUNNING")
