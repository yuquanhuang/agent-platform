"""Temporal run worker entrypoint."""

import asyncio

from apps.processes import ProcessName
from packages.contracts.temporal import TemporalWorkerKind
from packages.infrastructure.observability import (
    PlatformMetrics,
    configure_json_logging,
    configure_tracing,
)
from packages.infrastructure.public import get_settings
from packages.infrastructure.temporal import run_probe_worker_process


def main() -> None:
    settings = get_settings().model_copy(
        update={"service_name": ProcessName.TEMPORAL_WORKER_RUN.value}
    )
    configure_json_logging(
        service_name=settings.service_name,
        environment=settings.env.value,
        level=settings.log_level.value,
    )
    configure_tracing(
        service_name=settings.service_name,
        environment=settings.env.value,
        endpoint=(
            str(settings.otel_exporter_otlp_endpoint)
            if settings.otel_exporter_otlp_endpoint is not None
            else None
        ),
    )
    asyncio.run(
        run_probe_worker_process(
            settings,
            TemporalWorkerKind.RUN,
            PlatformMetrics(),
        )
    )


if __name__ == "__main__":
    main()
