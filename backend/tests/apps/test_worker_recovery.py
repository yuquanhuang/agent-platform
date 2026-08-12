"""Long-running worker dependency recovery tests."""

import asyncio
import signal
from collections.abc import Callable

import pytest

from apps import worker_recovery
from apps.worker_recovery import (
    WorkerRecoveryPolicy,
    is_retryable_worker_dependency_error,
    run_polling_worker_process,
    run_resilient_poll_loop,
)
from packages.contracts.public import dependency_unavailable, validation_error
from packages.infrastructure.observability import PlatformMetrics


def _counter(metrics: PlatformMetrics, outcome: str) -> float:
    value = metrics.registry.get_sample_value(
        "agent_platform_worker_cycles_total",
        labels={"process": "test-worker", "outcome": outcome},
    )
    return value if value is not None else 0.0


@pytest.mark.asyncio
async def test_retryable_dependency_failure_recovers_without_replaying_success() -> (
    None
):
    stop_event = asyncio.Event()
    metrics = PlatformMetrics()
    calls = 0

    async def cycle() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("database temporarily unavailable")
        stop_event.set()

    await run_resilient_poll_loop(
        cycle,
        stop_event,
        metrics,
        process_name="test-worker",
        poll_interval_seconds=1,
        recovery_policy=WorkerRecoveryPolicy(
            initial_delay_seconds=0.001,
            max_delay_seconds=0.002,
            max_consecutive_failures=3,
        ),
    )

    assert calls == 3
    assert _counter(metrics, "dependency_failure") == 2
    assert _counter(metrics, "completed") == 1
    assert _counter(metrics, "recovered") == 1
    assert metrics.registry.get_sample_value(
        "agent_platform_worker_consecutive_failures",
        labels={"process": "test-worker"},
    ) == pytest.approx(0)


@pytest.mark.asyncio
async def test_persistent_dependency_failure_exits_after_bounded_attempts() -> None:
    metrics = PlatformMetrics()
    calls = 0

    async def cycle() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("database did not respond")

    with pytest.raises(TimeoutError, match="did not respond"):
        await run_resilient_poll_loop(
            cycle,
            asyncio.Event(),
            metrics,
            process_name="test-worker",
            poll_interval_seconds=1,
            recovery_policy=WorkerRecoveryPolicy(
                initial_delay_seconds=0.001,
                max_delay_seconds=0.001,
                max_consecutive_failures=2,
            ),
        )

    assert calls == 2
    assert _counter(metrics, "dependency_failure") == 2
    assert _counter(metrics, "retry_exhausted") == 1


@pytest.mark.asyncio
async def test_unknown_cycle_error_fails_immediately() -> None:
    metrics = PlatformMetrics()
    calls = 0

    async def cycle() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("invalid durable fact")

    with pytest.raises(ValueError, match="invalid durable fact"):
        await run_resilient_poll_loop(
            cycle,
            asyncio.Event(),
            metrics,
            process_name="test-worker",
            poll_interval_seconds=1,
        )

    assert calls == 1
    assert _counter(metrics, "fatal_failure") == 1
    assert _counter(metrics, "dependency_failure") == 0


@pytest.mark.asyncio
async def test_shutdown_interrupts_dependency_backoff() -> None:
    stop_event = asyncio.Event()
    calls = 0

    async def cycle() -> None:
        nonlocal calls
        calls += 1
        stop_event.set()
        raise ConnectionError("dependency unavailable during shutdown")

    await run_resilient_poll_loop(
        cycle,
        stop_event,
        PlatformMetrics(),
        process_name="test-worker",
        poll_interval_seconds=1,
        recovery_policy=WorkerRecoveryPolicy(
            initial_delay_seconds=30,
            max_delay_seconds=30,
            max_consecutive_failures=2,
        ),
    )

    assert calls == 1


def test_only_retryable_dependency_platform_errors_enter_recovery() -> None:
    assert is_retryable_worker_dependency_error(
        dependency_unavailable("Temporal unavailable")
    )
    assert not is_retryable_worker_dependency_error(
        validation_error("invalid durable fact")
    )


class FakeSignalLoop:
    def __init__(self) -> None:
        self.handlers: dict[signal.Signals, Callable[[], None]] = {}
        self.removed: list[signal.Signals] = []

    def add_signal_handler(
        self, shutdown_signal: signal.Signals, callback: Callable[[], None]
    ) -> None:
        self.handlers[shutdown_signal] = callback

    def remove_signal_handler(self, shutdown_signal: signal.Signals) -> bool:
        self.removed.append(shutdown_signal)
        return self.handlers.pop(shutdown_signal, None) is not None


@pytest.mark.asyncio
async def test_process_lifecycle_sets_up_metric_and_handles_shutdown_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_loop = FakeSignalLoop()
    monkeypatch.setattr(worker_recovery.asyncio, "get_running_loop", lambda: fake_loop)
    metrics = PlatformMetrics()

    async def worker(stop_event: asyncio.Event) -> None:
        assert metrics.registry.get_sample_value(
            "agent_platform_process_up", labels={"process": "event-worker"}
        ) == pytest.approx(1)
        fake_loop.handlers[signal.SIGTERM]()
        await stop_event.wait()

    await run_polling_worker_process(
        worker,
        metrics,
        process_name="event-worker",
    )

    assert fake_loop.removed == [signal.SIGINT, signal.SIGTERM]
    assert metrics.registry.get_sample_value(
        "agent_platform_process_up", labels={"process": "event-worker"}
    ) == pytest.approx(0)
