"""API process entrypoint."""

import uvicorn

from packages.infrastructure.public import (
    configure_json_logging,
    configure_tracing,
    get_settings,
)


def main() -> None:
    """Run the local ASGI server using validated process settings."""

    settings = get_settings()
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
    uvicorn.run(
        "apps.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.value.lower(),
    )


if __name__ == "__main__":
    main()
