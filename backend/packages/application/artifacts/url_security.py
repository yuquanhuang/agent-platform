"""Signed Artifact URL validation independent from object-store SDKs."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit


@dataclass(frozen=True, slots=True)
class ArtifactGrantUrlPolicy:
    """Validate grants against an optional exact deployment origin allowlist."""

    allowed_origins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        normalized = frozenset(
            _normalized_configured_origin(origin) for origin in self.allowed_origins
        )
        object.__setattr__(self, "allowed_origins", normalized)

    def validate(self, url: str) -> None:
        parts = urlsplit(url)
        _validate_url_shape(parts)
        origin = _url_origin(parts)
        if self.allowed_origins:
            if origin not in self.allowed_origins:
                raise RuntimeError("Artifact object store URL origin is not allowed")
        elif parts.scheme != "https":
            raise RuntimeError("Artifact object store URL requires HTTPS")


def _normalized_configured_origin(origin: str) -> str:
    parts = urlsplit(origin)
    try:
        _validate_url_shape(parts)
    except RuntimeError as error:
        raise ValueError("Artifact public origin is invalid") from error
    if parts.path not in {"", "/"} or parts.query:
        raise ValueError("Artifact public origin cannot contain a path or query")
    return _url_origin(parts)


def _validate_url_shape(parts: SplitResult) -> None:
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(ord(character) < 32 for character in parts.geturl())
    ):
        raise RuntimeError("Artifact object store returned an unsafe URL")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RuntimeError("Artifact object store URL targets localhost")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return
    raise RuntimeError("Artifact object store URL cannot use an IP literal")


def _url_origin(parts: SplitResult) -> str:
    hostname = parts.hostname
    if hostname is None:
        raise RuntimeError("Artifact object store URL has no hostname")
    normalized_host = hostname.rstrip(".").lower().encode("idna").decode("ascii")
    try:
        port = parts.port
    except ValueError as error:
        raise RuntimeError("Artifact object store URL port is invalid") from error
    default_port = 443 if parts.scheme == "https" else 80
    suffix = "" if port in {None, default_port} else f":{port}"
    return f"{parts.scheme}://{normalized_host}{suffix}"
