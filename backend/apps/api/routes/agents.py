"""Frozen Agent Draft routes."""

from typing import Annotated, cast

from fastapi import APIRouter, Body, Header, Query, Request, Response, status

from packages.application.public import AgentManagementService, RequestMetadata
from packages.contracts.generated.core_models import (
    Agent,
    AgentCreateRequest,
    AgentPage,
    AgentUpdateRequest,
    CopyAgentRequest,
    DisableAgentRequest,
    OperationAccepted,
    ReferencePage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_agent_router(
    identity_provider: IdentityProvider | None,
    service: AgentManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    configured_service = cast(AgentManagementService, service)

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Agent Draft service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get(
        "/agents",
        tags=["Agents"],
        operation_id="listAgents",
        response_model=AgentPage,
    )
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        agent_status: Annotated[str | None, Query(alias="status")] = None,
        keyword: Annotated[str | None, Query(max_length=100)] = None,
    ) -> AgentPage:
        return await configured_service.list_agents(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            status=agent_status,
            keyword=keyword,
            metadata=metadata(request),
        )

    @router.post(
        "/agents",
        tags=["Agents"],
        operation_id="createAgent",
        response_model=Agent,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        response: Response,
        body: AgentCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Agent:
        result, etag = await configured_service.create_agent(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/agents/{agent_id}",
        tags=["Agents"],
        operation_id="getAgent",
        response_model=Agent,
    )
    async def get_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Agent:
        result, etag = await configured_service.get_agent(
            authenticate(authorization),
            agent_id=agent_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/agents/{agent_id}",
        tags=["Agents"],
        operation_id="updateAgent",
        response_model=Agent,
    )
    async def update_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        response: Response,
        body: AgentUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Agent:
        result, etag = await configured_service.update_agent(
            authenticate(authorization),
            agent_id=agent_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/agents/{agent_id}/references",
        tags=["Agents"],
        operation_id="listAgentReferences",
        response_model=ReferencePage,
    )
    async def list_agent_references(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ReferencePage:
        return await configured_service.list_agent_references(
            authenticate(authorization),
            agent_id=agent_id,
            metadata=metadata(request),
        )

    @router.post(
        "/agents/{agent_id}/copy",
        tags=["Agents"],
        operation_id="copyAgent",
        response_model=Agent,
        status_code=status.HTTP_201_CREATED,
    )
    async def copy_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        response: Response,
        body: CopyAgentRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Agent:
        result, etag = await configured_service.copy_agent(
            authenticate(authorization),
            agent_id=agent_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/agents/{agent_id}/disable",
        tags=["Agents"],
        operation_id="disableAgent",
        response_model=Agent,
    )
    async def disable_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        body: Annotated[DisableAgentRequest | None, Body()] = None,
    ) -> Agent:
        result, etag = await configured_service.disable_agent(
            authenticate(authorization),
            agent_id=agent_id,
            if_match=if_match,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/agents/{agent_id}",
        tags=["Agents"],
        operation_id="deleteAgent",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured_service.delete_agent(
            authenticate(authorization),
            agent_id=agent_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    return router
