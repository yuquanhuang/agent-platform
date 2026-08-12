"""Stable application errors mapped to the frozen API error envelope."""

from collections.abc import Mapping


class PlatformError(Exception):
    """Safe error carrying stable machine-readable API semantics."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details) if details is not None else None
        self.headers = dict(headers) if headers is not None else None


def unauthenticated(message: str = "Authentication is required.") -> PlatformError:
    return PlatformError(status_code=401, code="UNAUTHENTICATED", message=message)


def permission_denied(message: str = "Permission is denied.") -> PlatformError:
    return PlatformError(status_code=403, code="PERMISSION_DENIED", message=message)


def dependency_unavailable(
    message: str = "A required dependency is unavailable.",
) -> PlatformError:
    return PlatformError(
        status_code=503,
        code="DEPENDENCY_UNAVAILABLE",
        message=message,
        retryable=True,
    )


def validation_error(message: str) -> PlatformError:
    return PlatformError(status_code=400, code="VALIDATION_ERROR", message=message)


def resource_not_found(message: str = "Resource was not found.") -> PlatformError:
    return PlatformError(status_code=404, code="RESOURCE_NOT_FOUND", message=message)


def resource_state_conflict(message: str) -> PlatformError:
    return PlatformError(
        status_code=409, code="RESOURCE_STATE_CONFLICT", message=message
    )


def run_already_active(
    message: str = "The Session branch already has an active Run.",
) -> PlatformError:
    return PlatformError(status_code=409, code="RUN_ALREADY_ACTIVE", message=message)


def rate_limited(
    message: str = "Request capacity is temporarily exhausted.",
    *,
    details: Mapping[str, object] | None = None,
) -> PlatformError:
    return PlatformError(
        status_code=429,
        code="RATE_LIMITED",
        message=message,
        retryable=True,
        details=details,
    )


def range_not_satisfiable(*, total_size: int) -> PlatformError:
    if total_size < 0:
        raise ValueError("total_size must not be negative")
    return PlatformError(
        status_code=416,
        code="RANGE_NOT_SATISFIABLE",
        message="The requested Artifact byte range cannot be satisfied.",
        details={"total_size": total_size},
        headers={"Content-Range": f"bytes */{total_size}"},
    )


def run_event_sequence_gap(
    *,
    after: int,
    expected_sequence_no: int,
    observed_sequence_no: int | None,
    latest_sequence_no: int,
) -> PlatformError:
    return PlatformError(
        status_code=409,
        code="RUN_EVENT_SEQUENCE_GAP",
        message="Run event history is incomplete and cannot be replayed safely.",
        details={
            "after": after,
            "expected_sequence_no": expected_sequence_no,
            "observed_sequence_no": observed_sequence_no,
            "latest_sequence_no": latest_sequence_no,
        },
    )


def idempotency_key_reused() -> PlatformError:
    return PlatformError(
        status_code=409,
        code="IDEMPOTENCY_KEY_REUSED",
        message="Idempotency key was reused for a different request.",
    )


def resource_version_conflict() -> PlatformError:
    return PlatformError(
        status_code=412,
        code="RESOURCE_VERSION_CONFLICT",
        message="Resource version does not match If-Match.",
    )
