"""Compatibility entrypoint for the Agent Platform API process."""

from apps.api.main import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
