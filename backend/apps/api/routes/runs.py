"""Frozen Run creation and query routes."""

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from starlette.responses import Response, StreamingResponse

from apps.api.sse import BoundedSseResponse
from packages.application.public import (
    RequestMetadata,
    RunEventQueryService,
    RunEventStreamService,
    RunManagementService,
)
from packages.contracts.generated.core_models import (
    CancelRunRequest,
    RetryRunRequest,
    Run,
    RunAccepted,
    RunCreateRequest,
    RunEventPage,
    RunPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
    validation_error,
)


def create_run_router(
    identity_provider: IdentityProvider | None,
    service: RunManagementService | None,
    event_query_service: RunEventQueryService | None = None,
    event_stream_service: RunEventStreamService | None = None,
    sse_send_timeout_seconds: float = 15.0,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(
        authorization: str | None,
    ) -> tuple[AuthenticatedPrincipal, RunManagementService]:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Run service is not configured.")
        return identity_provider.authenticate(authorization), service

    def authenticate_event_query(
        authorization: str | None,
    ) -> tuple[AuthenticatedPrincipal, RunEventQueryService]:
        if identity_provider is None or event_query_service is None:
            raise dependency_unavailable("Run event query service is not configured.")
        return identity_provider.authenticate(authorization), event_query_service

    def authenticate_event_stream(
        authorization: str | None,
    ) -> tuple[AuthenticatedPrincipal, RunEventStreamService]:
        if identity_provider is None or event_stream_service is None:
            raise dependency_unavailable("Run event stream service is not configured.")
        return identity_provider.authenticate(authorization), event_stream_service

    @router.get(
        "/sessions/{session_id}/runs",
        tags=["Runs"],
        operation_id="listSessionRuns",
        response_model=RunPage,
    )
    async def list_session_runs(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> RunPage:
        principal, configured_service = authenticate(authorization)
        return await configured_service.list_session_runs(
            principal,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.post(
        "/runs",
        tags=["Runs"],
        operation_id="createRun",
        response_model=RunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        body: RunCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> RunAccepted:
        principal, configured_service = authenticate(authorization)
        return await configured_service.create_run(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/runs/{run_id}",
        tags=["Runs"],
        operation_id="getRun",
        response_model=Run,
    )
    async def get_run(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Run:
        principal, configured_service = authenticate(authorization)
        return await configured_service.get_run(
            principal, run_id=run_id, metadata=metadata(request)
        )

    @router.get(
        "/runs/{run_id}/events",
        tags=["Runs"],
        operation_id="listRunEvents",
        response_model=RunEventPage,
    )
    async def list_run_events(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> RunEventPage:
        principal, configured_service = authenticate_event_query(authorization)
        return await configured_service.list_events(
            principal,
            run_id=run_id,
            after=after,
            limit=limit,
            metadata=metadata(request),
        )

    @router.get(
        "/runs/{run_id}/events/stream",
        include_in_schema=False,
    )
    @router.get(
        "/runs/{run_id}/stream",
        tags=["Runs"],
        operation_id="streamRunEvents",
        response_class=StreamingResponse,
    )
    async def stream_run_events(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        principal, configured_service = authenticate_event_stream(authorization)
        event_stream = await configured_service.open_stream(
            principal,
            run_id=run_id,
            after=_stream_after(last_event_id, after),
            metadata=metadata(request),
        )
        return BoundedSseResponse(
            event_stream,
            send_timeout_seconds=sse_send_timeout_seconds,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/runs/{run_id}/cancel",
        tags=["Runs"],
        operation_id="cancelRun",
        response_model=Run,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_run(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: CancelRunRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Run:
        principal, configured_service = authenticate(authorization)
        return await configured_service.cancel_run(
            principal,
            run_id=run_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/runs/{run_id}/retry",
        tags=["Runs"],
        operation_id="retryRun",
        response_model=RunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_run(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: Request,
        body: RetryRunRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> RunAccepted:
        principal, configured_service = authenticate(authorization)
        return await configured_service.retry_run(
            principal,
            run_id=run_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    return router


def _stream_after(last_event_id: str | None, after: int | None) -> int:
    if last_event_id is None:
        return after or 0
    try:
        parsed = int(last_event_id)
    except ValueError as error:
        raise validation_error(
            "Last-Event-ID must be a non-negative integer."
        ) from error
    if parsed < 0 or str(parsed) != last_event_id:
        raise validation_error("Last-Event-ID must be a non-negative integer.")
    return parsed
