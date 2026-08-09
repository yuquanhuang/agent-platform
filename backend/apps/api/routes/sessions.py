"""Frozen Session management routes."""

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status

from packages.application.public import (
    MessageHistoryService,
    RequestMetadata,
    SessionManagementService,
)
from packages.contracts.generated.core_models import (
    MessagePage,
    OperationAccepted,
    Session,
    SessionCreateRequest,
    SessionPage,
    SessionUpdateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_session_router(
    identity_provider: IdentityProvider | None,
    service: SessionManagementService | None,
    message_service: MessageHistoryService | None,
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
    ) -> tuple[AuthenticatedPrincipal, SessionManagementService]:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Session service is not configured.")
        return identity_provider.authenticate(authorization), service

    def authenticate_messages(
        authorization: str | None,
    ) -> tuple[AuthenticatedPrincipal, MessageHistoryService]:
        if identity_provider is None or message_service is None:
            raise dependency_unavailable("Message history service is not configured.")
        return identity_provider.authenticate(authorization), message_service

    @router.get(
        "/sessions",
        tags=["Sessions"],
        operation_id="listSessions",
        response_model=SessionPage,
    )
    async def list_sessions(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        agent_id: Annotated[str | None, Query()] = None,
        session_status: Annotated[str | None, Query(alias="status")] = None,
    ) -> SessionPage:
        principal, configured_service = authenticate(authorization)
        return await configured_service.list_sessions(
            principal,
            limit=limit,
            cursor=cursor,
            agent_id=agent_id,
            status=session_status,
            metadata=metadata(request),
        )

    @router.post(
        "/sessions",
        tags=["Sessions"],
        operation_id="createSession",
        response_model=Session,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        response: Response,
        body: SessionCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Session:
        principal, configured_service = authenticate(authorization)
        result, etag = await configured_service.create_session(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/sessions/{session_id}",
        tags=["Sessions"],
        operation_id="getSession",
        response_model=Session,
    )
    async def get_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Session:
        principal, configured_service = authenticate(authorization)
        result, etag = await configured_service.get_session(
            principal,
            session_id=session_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/sessions/{session_id}",
        tags=["Sessions"],
        operation_id="updateSession",
        response_model=Session,
    )
    async def update_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        response: Response,
        body: SessionUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Session:
        principal, configured_service = authenticate(authorization)
        result, etag = await configured_service.update_session(
            principal,
            session_id=session_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/sessions/{session_id}/messages",
        tags=["Sessions"],
        operation_id="listSessionMessages",
        response_model=MessagePage,
    )
    async def list_session_messages(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        branch_id: Annotated[str | None, Query()] = None,
    ) -> MessagePage:
        principal, configured_message_service = authenticate_messages(authorization)
        return await configured_message_service.list_session_messages(
            principal,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            branch_id=branch_id,
            metadata=metadata(request),
        )

    @router.post(
        "/sessions/{session_id}/archive",
        tags=["Sessions"],
        operation_id="archiveSession",
        response_model=Session,
    )
    async def archive_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Session:
        principal, configured_service = authenticate(authorization)
        result, etag = await configured_service.archive_session(
            principal,
            session_id=session_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/sessions/{session_id}",
        tags=["Sessions"],
        operation_id="deleteSession",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        principal, configured_service = authenticate(authorization)
        return await configured_service.delete_session(
            principal,
            session_id=session_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    return router
