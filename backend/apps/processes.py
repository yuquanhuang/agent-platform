"""Shared behavior for process boundaries not implemented in this task."""

from enum import StrEnum
from typing import NoReturn


class ProcessName(StrEnum):
    TEMPORAL_WORKER_CONTROL = "temporal-worker-control"
    TEMPORAL_WORKER_RUN = "temporal-worker-run"
    RUNTIME_WORKER_AGENTSCOPE = "runtime-worker-agentscope"
    RUNTIME_WORKER_CODEX = "runtime-worker-codex"
    EVENT_WORKER = "event-worker"
    SANDBOX_MANAGER = "sandbox-manager"
    RECONCILIATION_WORKER = "reconciliation-worker"


def unavailable_process(process_name: ProcessName) -> NoReturn:
    """Fail closed until the process receives its dedicated implementation."""

    raise SystemExit(
        f"{process_name} is reserved but not implemented in AP-E0-002; "
        "run its dedicated Epic task before deployment."
    )
