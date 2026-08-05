"""Minimal Epic 0 health checks for the backend package."""

from main import main


def test_backend_entrypoint_is_importable() -> None:
    assert callable(main)
