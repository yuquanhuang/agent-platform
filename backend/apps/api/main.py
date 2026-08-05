"""API process entrypoint."""

import uvicorn

from packages.infrastructure.public import get_settings


def main() -> None:
    """Run the local ASGI server using validated process settings."""

    settings = get_settings()
    uvicorn.run(
        "apps.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.value.lower(),
    )


if __name__ == "__main__":
    main()
