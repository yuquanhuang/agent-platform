"""Bounded recovery loop shared by long-running polling workers."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from temporalio.service import RPCError

from packages.contracts.public import PlatformError
from packages.infrastructure.observability import PlatformMetrics

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerRecoveryPolicy:
    """Bound retries so an orchestrator can replace a persistently unhealthy worker."""

    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    max_consecutive_failures: int = 5

    def __post_init__(self) -> None:
        if self.initial_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("Worker recovery delays must be positive")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("Worker initial recovery delay cannot exceed its maximum")
        if self.max_consecutive_failures < 1:
            raise ValueError("Worker consecutive failure limit must be positive")

    def delay_for(self, consecutive_failures: int) -> float:
        if consecutive_failures < 1:
            raise ValueError("consecutive_failures must be positive")
        return min(
            self.initial_delay_seconds * (2 ** (consecutive_failures - 1)),
            self.max_delay_seconds,
        )


def is_retryable_worker_dependency_error(error: Exception) -> bool:
    """Recognize transport/database failures without retrying arbitrary code bugs."""

    if isinstance(error, PlatformError):
        return (
            error.status_code == 503
            and error.code == "DEPENDENCY_UNAVAILABLE"
            and error.retryable
        )
    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            OSError,
            RedisError,
            RPCError,
            InterfaceError,
            OperationalError,
        ),
    ):
        return True
    return isinstance(error, DBAPIError) and error.connection_invalidated


async def run_polling_worker_process(
    worker_loop: Callable[[asyncio.Event], Awaitable[None]],
    metrics: PlatformMetrics,
    *,
    process_name: str,
    shutdown_signals: tuple[signal.Signals, ...] = (
        signal.SIGINT,
        signal.SIGTERM,
    ),
) -> None:
    """Expose one graceful process lifecycle around an explicitly composed loop."""

    if not process_name.strip():
        raise ValueError("process_name must not be empty")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    try:
        for shutdown_signal in shutdown_signals:
            loop.add_signal_handler(shutdown_signal, stop_event.set)
            installed_signals.append(shutdown_signal)
        metrics.process_up.labels(process=process_name).set(1)
        await worker_loop(stop_event)
    finally:
        metrics.process_up.labels(process=process_name).set(0)
        for shutdown_signal in installed_signals:
            loop.remove_signal_handler(shutdown_signal)


async def run_resilient_poll_loop(
    cycle: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
    metrics: PlatformMetrics,
    *,
    process_name: str,
    poll_interval_seconds: float,
    recovery_policy: WorkerRecoveryPolicy | None = None,
    is_retryable: Callable[[Exception], bool] = is_retryable_worker_dependency_error,
) -> None:
    """Run cycles with interruptible bounded backoff and fail-closed exhaustion."""

    if not process_name.strip():
        raise ValueError("process_name must not be empty")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    policy = recovery_policy or WorkerRecoveryPolicy()
    consecutive_failures = 0

    while not stop_event.is_set():
        try:
            await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not is_retryable(error):
                metrics.observe_worker_cycle(
                    process=process_name, outcome="fatal_failure"
                )
                raise
            consecutive_failures += 1
            metrics.set_worker_consecutive_failures(
                process=process_name, count=consecutive_failures
            )
            metrics.observe_worker_cycle(
                process=process_name, outcome="dependency_failure"
            )
            if consecutive_failures >= policy.max_consecutive_failures:
                metrics.observe_worker_cycle(
                    process=process_name, outcome="retry_exhausted"
                )
                LOGGER.exception(
                    "Worker dependency recovery exhausted process=%s failures=%d",
                    process_name,
                    consecutive_failures,
                )
                raise
            delay = policy.delay_for(consecutive_failures)
            LOGGER.warning(
                "Worker dependency failure process=%s failures=%d retry_delay_seconds=%s",
                process_name,
                consecutive_failures,
                delay,
                exc_info=True,
            )
            await _wait_for_stop(stop_event, delay)
            continue

        metrics.observe_worker_cycle(process=process_name, outcome="completed")
        if consecutive_failures:
            metrics.observe_worker_cycle(process=process_name, outcome="recovered")
            consecutive_failures = 0
            metrics.set_worker_consecutive_failures(process=process_name, count=0)
        await _wait_for_stop(stop_event, poll_interval_seconds)


async def _wait_for_stop(stop_event: asyncio.Event, timeout_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return
