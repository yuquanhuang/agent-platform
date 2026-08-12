"""Request correlation and frozen error-envelope handling."""

import logging
import re
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from packages.contracts.public import PlatformError
from packages.infrastructure.observability import PlatformMetrics, bind_log_context

LOGGER = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def request_id_from(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else f"req_{uuid4().hex}"


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request_id_from(request),
        "retryable": retryable,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


async def handle_platform_error(request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, PlatformError):
        raise TypeError("platform error handler received unexpected exception")
    return error_response(
        request,
        status_code=exception.status_code,
        code=exception.code,
        message=exception.message,
        retryable=exception.retryable,
        details=exception.details,
        headers=exception.headers,
    )


async def handle_validation_error(
    request: Request, exception: Exception
) -> JSONResponse:
    if not isinstance(exception, RequestValidationError):
        raise TypeError("validation handler received unexpected exception")
    safe_errors = [
        {
            "location": ".".join(str(part) for part in error["loc"]),
            "type": error["type"],
        }
        for error in exception.errors()
    ]
    return error_response(
        request,
        status_code=422,
        code="CONTRACT_VALIDATION_FAILED",
        message="Request does not match the API contract.",
        details={"errors": safe_errors},
    )


async def handle_http_error(request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, StarletteHTTPException):
        raise TypeError("HTTP error handler received unexpected exception")
    code = "RESOURCE_NOT_FOUND" if exception.status_code == 404 else "HTTP_ERROR"
    return error_response(
        request,
        status_code=exception.status_code,
        code=code,
        message="The requested resource is unavailable.",
    )


async def handle_unexpected_error(
    request: Request, exception: Exception
) -> JSONResponse:
    LOGGER.exception(
        "Unhandled API error request_id=%s path=%s",
        request_id_from(request),
        request.url.path,
        exc_info=exception,
    )
    return error_response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Create trusted correlation identifiers and expose X-Request-ID."""

    def __init__(self, app: ASGIApp, *, metrics: PlatformMetrics) -> None:
        super().__init__(app)
        self._metrics = metrics
        self._tracer = trace.get_tracer("agent-platform.api")

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID")
        if supplied_request_id and REQUEST_ID_PATTERN.fullmatch(supplied_request_id):
            request_id = supplied_request_id
        else:
            request_id = f"req_{uuid4().hex}"
        with self._tracer.start_as_current_span(
            "http.request", attributes={"http.request.method": request.method}
        ):
            span_context = trace.get_current_span().get_span_context()
            trace_id = (
                f"{span_context.trace_id:032x}"
                if span_context.is_valid
                else f"trace_{uuid4().hex}"
            )
            request.state.request_id = request_id
            request.state.trace_id = trace_id
            started_at = time.perf_counter()
            with bind_log_context(request_id=request_id, trace_id=trace_id):
                response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            self._metrics.observe_http(
                method=request.method,
                status_code=response.status_code,
                duration=time.perf_counter() - started_at,
            )
            return response


def install_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(PlatformError, handle_platform_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(StarletteHTTPException, handle_http_error)
    application.add_exception_handler(Exception, handle_unexpected_error)
