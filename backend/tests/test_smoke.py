"""Smoke checks for the backend package entrypoint."""

from main import main


def test_backend_entrypoint_is_importable() -> None:
    assert callable(main)
